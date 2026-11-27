"""SQLite persistence for trusted durable-run service operations."""

from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
import sqlite3
from time import time
from typing import Any
from uuid import uuid4

from avi.agents.base import RunStatus
from avi.core.durable_auth import AccessDenied
from avi.core.lifecycle import AGENT_RUN_TRANSITIONS, AgentRunState


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SCHEMA_VERSION = 2
_SCHEMA = (
    """
    CREATE TABLE durable_runs (
        run_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        request_id TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        backend_scope TEXT NOT NULL,
        dispatch TEXT NOT NULL,
        state TEXT NOT NULL,
        handle TEXT,
        result TEXT,
        progress TEXT,
        failure_code TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        terminal_at REAL,
        cursor INTEGER NOT NULL DEFAULT 0,
        reconciliation_pending INTEGER NOT NULL DEFAULT 0,
        expiry_noted INTEGER NOT NULL DEFAULT 0,
        expiry_cancel_requested INTEGER NOT NULL DEFAULT 0,
        UNIQUE (owner, request_id)
    )
    """,
    """
    CREATE TABLE durable_run_events (
        run_id TEXT NOT NULL REFERENCES durable_runs(run_id) ON DELETE CASCADE,
        cursor INTEGER NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (run_id, cursor)
    )
    """,
    """
    CREATE TABLE legacy_cutover_outcomes (
        session_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'cancelled')),
        observed_at REAL NOT NULL,
        PRIMARY KEY (session_id, run_id)
    )
    """,
)

# Version 2 adds write-ahead records for legacy work tracked by this gateway.
_LEGACY_SCHEMA = (
    "CREATE TABLE legacy_freeze (singleton INTEGER PRIMARY KEY CHECK (singleton = 1))",
    """CREATE TABLE legacy_admissions (
        ticket TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        run_id TEXT,
        UNIQUE (session_id, run_id)
    )""",
)

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class DispatchState(StrEnum):
    RESERVED = "reserved"
    DISPATCHING = "dispatching"
    ACCEPTED = "accepted"
    UNCERTAIN = "uncertain"
    CLOSED = "closed"


class AdmissionsClosed(ValueError):
    pass


class RunConflict(ValueError):
    pass


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class Retention:
    execution_ttl: float = 600
    results_ttl: float = 3600
    retry_window: float = 86400
    history_limit: int = 128

    def __post_init__(self):
        for value in (self.execution_ttl, self.results_ttl, self.retry_window):
            if not isfinite(value) or value <= 0:
                raise ValueError("Retention times must be positive and finite")
        if self.retry_window < self.results_ttl:
            raise ValueError("Retry window must cover result retention")
        if type(self.history_limit) is not int or not 1 <= self.history_limit <= 128:
            raise ValueError("History limit must be between 1 and 128 events")


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    state: AgentRunState
    dispatch: DispatchState
    cursor: int
    expires_at: float
    expired: bool
    failure_code: str | None
    reconciliation_pending: bool


@dataclass(frozen=True)
class RecentRun:
    run_id: str
    state: AgentRunState


@dataclass(frozen=True)
class RunResult:
    status: RunSnapshot
    result: str | None
    retained: bool


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    cursor: int
    kind: str
    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class RunReplay:
    events: tuple[RunEvent, ...]
    snapshot: RunResult | None
    resume_cursor: int


@dataclass(frozen=True)
class Observation:
    handle: str = field(repr=False)
    state: AgentRunState
    result: str | None = field(default=None, repr=False)
    progress: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.state, AgentRunState):
            raise ValueError("Invalid observed state")
        if not isinstance(self.handle, str) or not self.handle.strip():
            raise ValueError("Invalid provider handle")
        _bounded_text(self.handle, 512)
        _bounded_text(self.progress, 4096)
        _bounded_text(self.result, 4096)
        if self.result is not None and self.state is not AgentRunState.COMPLETED:
            raise ValueError("Result requires confirmed completion")


@dataclass(frozen=True)
class DispatchIntent:
    owner: str
    status: RunSnapshot
    handle: str | None = field(repr=False)
    backend_scope: str = field(repr=False)


@dataclass(frozen=True)
class LegacyOutcome:
    session_id: str
    run_id: str
    status: RunStatus


def _bounded_text(value: str | None, limit: int) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Invalid provider field")
    try:
        encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Invalid provider field") from None
    if len(encoded) > 12000:
        raise ValueError("Invalid provider field")


def canonical_task(kind: str, arguments: dict[str, JsonValue]) -> str:
    if not isinstance(kind, str) or _IDENTIFIER.fullmatch(kind) is None:
        raise ValueError("Invalid task kind")
    if not isinstance(arguments, dict):
        raise ValueError("Invalid task arguments")
    try:
        payload = json.dumps(
            {"kind": kind, "arguments": arguments},
            allow_nan=False, sort_keys=True, separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Invalid task arguments") from None
    if len(payload) > 4096:
        raise ValueError("Task exceeds payload limit")
    return payload


class SQLiteRunStore:
    def __init__(
        self,
        path: Path,
        *,
        retention: Retention = Retention(),
        clock: Callable[[], float] = time,
    ):
        self.retention = retention
        self._path = path.resolve()
        self._clock = clock
        # In addition to event metadata, a database may contain retained task results.
        os.close(os.open(self._path, os.O_CREAT | os.O_RDWR, mode=0o600))
        with self._transaction(write=True) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA:
                    db.execute(statement)
            if version in (0, 1):
                for statement in _LEGACY_SCHEMA:
                    db.execute(statement)
                # Historical cutover receipts prove the old gate was frozen.
                db.execute("INSERT INTO legacy_freeze SELECT 1 WHERE EXISTS "
                           "(SELECT 1 FROM legacy_cutover_outcomes)")
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            elif version != _SCHEMA_VERSION:
                raise ValueError("Unsupported durable-run database schema")
            db.execute(
                "CREATE INDEX IF NOT EXISTS recent_runs_by_owner "
                "ON durable_runs(owner, created_at DESC, run_id DESC)"
            )

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()

    @staticmethod
    def _get(db: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        if not isinstance(run_id, str) or _IDENTIFIER.fullmatch(run_id) is None:
            raise AccessDenied()
        row = db.execute("SELECT * FROM durable_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise AccessDenied()
        return dict(row)

    def _owned(self, db: sqlite3.Connection, owner: str, run_id: str) -> dict[str, Any]:
        row = self._get(db, run_id)
        if row["owner"] != owner:
            raise AccessDenied()
        return row

    def _snapshot(self, row: dict[str, Any], now: float) -> RunSnapshot:
        return RunSnapshot(
            run_id=row["run_id"],
            state=AgentRunState(row["state"]),
            dispatch=DispatchState(row["dispatch"]),
            cursor=row["cursor"],
            expires_at=row["expires_at"],
            expired=bool(row["expiry_noted"]) or (
                row["terminal_at"] is None and row["expires_at"] <= now
            ),
            failure_code=row["failure_code"],
            reconciliation_pending=bool(row["reconciliation_pending"]),
        )

    def _result(self, row: dict[str, Any], now: float, include_result: bool) -> RunResult:
        terminal_at = row["terminal_at"]
        retained = terminal_at is not None and now < terminal_at + self.retention.results_ttl
        return RunResult(
            status=self._snapshot(row, now),
            result=row["result"] if retained and include_result else None,
            retained=retained,
        )

    def _record(
        self,
        db: sqlite3.Connection,
        row: dict[str, Any],
        events: list[tuple[str, dict[str, JsonValue]]],
        now: float,
    ) -> None:
        for kind, payload in events:
            row["cursor"] += 1
            db.execute(
                "INSERT INTO durable_run_events VALUES (?, ?, ?, ?)",
                (row["run_id"], row["cursor"], kind, json.dumps(payload, ensure_ascii=False)),
            )
        row["updated_at"] = now
        db.execute(
            """
            UPDATE durable_runs SET dispatch = :dispatch, state = :state, handle = :handle,
                result = :result, progress = :progress, failure_code = :failure_code,
                updated_at = :updated_at, terminal_at = :terminal_at, cursor = :cursor,
                reconciliation_pending = :reconciliation_pending, expiry_noted = :expiry_noted,
                expiry_cancel_requested = :expiry_cancel_requested
            WHERE run_id = :run_id
            """,
            row,
        )
        db.execute(
            "DELETE FROM durable_run_events WHERE run_id = ? AND cursor <= ?",
            (row["run_id"], row["cursor"] - self.retention.history_limit),
        )

    def reserve(
        self,
        owner: str,
        request_id: str,
        kind: str,
        arguments: dict[str, JsonValue],
        *,
        allow_new: bool,
        backend_scope: str,
    ) -> RunSnapshot:
        if not isinstance(request_id, str) or _IDENTIFIER.fullmatch(request_id) is None:
            raise ValueError("Invalid request ID")
        digest = sha256(canonical_task(kind, arguments).encode("ascii")).hexdigest()
        with self._transaction(write=True) as db:
            now = self._clock()
            existing = db.execute(
                "SELECT * FROM durable_runs WHERE owner = ? AND request_id = ?", (owner, request_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_digest"] != digest:
                    raise RunConflict("Request ID is bound to a different task")
                return self._snapshot(dict(existing), now)
            if not allow_new:
                raise AdmissionsClosed("Durable submissions are disabled")
            run_id = uuid4().hex
            db.execute(
                """
                INSERT INTO durable_runs (
                    run_id, owner, request_id, payload_digest, backend_scope, dispatch, state,
                    created_at, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, owner, request_id, digest, backend_scope, DispatchState.RESERVED,
                 AgentRunState.SUBMITTED, now, now, now + self.retention.execution_ttl),
            )
            row = self._get(db, run_id)
            self._record(db, row, [("submitted", {})], now)
            return self._snapshot(row, now)

    def claim_dispatch(self, run_id: str, *, backend_scope: str) -> bool:
        with self._transaction(write=True) as db:
            row = self._get(db, run_id)
            now = self._clock()
            if (
                row["dispatch"] != DispatchState.RESERVED
                or row["expires_at"] <= now
                or row["backend_scope"] != backend_scope
            ):
                return False
            row["dispatch"] = DispatchState.DISPATCHING
            self._record(db, row, [("dispatching", {})], now)
            return True

    def dispatch_error(self, run_id: str, *, outcome: str) -> RunSnapshot:
        with self._transaction(write=True) as db:
            row = self._get(db, run_id)
            now = self._clock()
            if row["dispatch"] != DispatchState.DISPATCHING:
                return self._snapshot(row, now)
            events = self._expiry_events(row, now)
            if outcome == "uncertain":
                row.update(dispatch=DispatchState.UNCERTAIN, reconciliation_pending=1)
                events.append(("dispatch_uncertain", {}))
            else:
                row.update(
                    dispatch=DispatchState.CLOSED, state=AgentRunState.FAILED,
                    terminal_at=now, failure_code=f"dispatch_{outcome}",
                )
                events.append(("failed", {"code": row["failure_code"]}))
            self._record(db, row, events, now)
            return self._snapshot(row, now)

    def mark_reconciliation_pending(self, run_id: str) -> None:
        with self._transaction(write=True) as db:
            row = self._get(db, run_id)
            if row["terminal_at"] is None and not row["reconciliation_pending"]:
                row["reconciliation_pending"] = 1
                self._record(db, row, [("reconciliation_pending", {})], self._clock())

    def record_observation(self, run_id: str, observation: Observation) -> RunSnapshot:
        with self._transaction(write=True) as db:
            row = self._get(db, run_id)
            now = self._clock()
            if row["terminal_at"] is not None:
                return self._snapshot(row, now)
            if row["dispatch"] == DispatchState.RESERVED:
                raise ValueError("Dispatch was never claimed")
            if row["handle"] not in (None, observation.handle):
                return self._snapshot(row, now)
            events = self._expiry_events(row, now)
            if row["reconciliation_pending"]:
                row["reconciliation_pending"] = 0
                events.append(("reconciled", {}))
            if row["dispatch"] != DispatchState.ACCEPTED:
                row.update(dispatch=DispatchState.ACCEPTED, handle=observation.handle)
                events.append(("accepted", {}))
            current = AgentRunState(row["state"])
            target = observation.state
            # A directly observed completion confirms that the run executed.
            if target is AgentRunState.COMPLETED and current in (
                AgentRunState.SUBMITTED, AgentRunState.QUEUED,
            ):
                self._transition(row, AgentRunState.RUNNING, None, now, events)
                current = AgentRunState.RUNNING
            if target in AGENT_RUN_TRANSITIONS[current]:
                self._transition(row, target, observation.result, now, events)
            if target is AgentRunState.RUNNING and row["state"] == target:
                if observation.progress is not None and row["progress"] != observation.progress:
                    row["progress"] = observation.progress
                    events.append(("progress", {"text": observation.progress}))
            if events:
                self._record(db, row, events, now)
            return self._snapshot(row, now)

    @staticmethod
    def _transition(
        row: dict[str, Any],
        target: AgentRunState,
        result: str | None,
        now: float,
        events: list[tuple[str, dict[str, JsonValue]]],
    ) -> None:
        row["state"] = target
        payload: dict[str, JsonValue] = {}
        if target is AgentRunState.COMPLETED:
            row["result"] = result
            payload["result"] = result
        elif target is AgentRunState.FAILED:
            row["failure_code"] = "provider_failed"
            payload["code"] = row["failure_code"]
        if not AGENT_RUN_TRANSITIONS[target]:
            row["terminal_at"] = now
        events.append((target.value, payload))

    def lookup_admitted(
        self, owner: str, request_id: str, kind: str, arguments: dict[str, JsonValue],
        *, backend_scope: str,
    ) -> RunSnapshot | None:
        """Read proved admission without reserving or exposing a provider handle."""
        if not isinstance(request_id, str) or _IDENTIFIER.fullmatch(request_id) is None:
            return None
        digest = sha256(canonical_task(kind, arguments).encode("ascii")).hexdigest()
        with self._transaction() as db:
            row = db.execute(
                """
                SELECT * FROM durable_runs
                 WHERE owner = ? AND request_id = ? AND payload_digest = ?
                   AND backend_scope = ? AND dispatch = ? AND handle IS NOT NULL
                """,
                (owner, request_id, digest, backend_scope, DispatchState.ACCEPTED),
            ).fetchone()
            return None if row is None else self._snapshot(dict(row), self._clock())

    def recent(self, owner: str) -> tuple[RecentRun, ...]:
        with self._transaction() as db:
            rows = db.execute(
                """
                SELECT run_id, state FROM durable_runs
                WHERE owner = ? AND (terminal_at IS NULL OR terminal_at > ?)
                ORDER BY created_at DESC, run_id DESC LIMIT 16
                """,
                (owner, self._clock() - self.retention.results_ttl),
            )
            return tuple(RecentRun(row["run_id"], AgentRunState(row["state"])) for row in rows)

    def intent(self, owner: str, run_id: str) -> DispatchIntent:
        with self._transaction() as db:
            row = self._owned(db, owner, run_id)
            return self._intent(row, self._clock())

    def _intent(self, row: dict[str, Any], now: float) -> DispatchIntent:
        return DispatchIntent(row["owner"], self._snapshot(row, now), row["handle"], row["backend_scope"])

    def result(self, owner: str, run_id: str) -> RunResult:
        with self._transaction() as db:
            return self._result(self._owned(db, owner, run_id), self._clock(), include_result=True)

    def replay(
        self, owner: str, run_id: str, *, after_cursor: int, include_result: bool,
    ) -> RunReplay:
        with self._transaction() as db:
            row = self._owned(db, owner, run_id)
            cursor = row["cursor"]
            if type(after_cursor) is not int or not 0 <= after_cursor <= cursor:
                raise InvalidCursor("Cursor is outside the run history")
            now = self._clock()
            snapshot = self._result(row, now, include_result)
            if after_cursor == cursor:
                return RunReplay((), None, cursor)
            if row["terminal_at"] is not None and not snapshot.retained:
                return RunReplay((), snapshot, cursor)
            rows = db.execute(
                """
                SELECT cursor, kind, payload FROM durable_run_events
                WHERE run_id = ? AND cursor > ? ORDER BY cursor
                """,
                (run_id, after_cursor),
            ).fetchall()
            if not rows or rows[0]["cursor"] != after_cursor + 1:
                return RunReplay((), snapshot, cursor)
            events = []
            for event in rows:
                payload = json.loads(event["payload"])
                if not include_result:
                    payload.pop("result", None)
                events.append(RunEvent(run_id, event["cursor"], event["kind"], payload))
            return RunReplay(tuple(events), None, cursor)

    def pending(self) -> tuple[DispatchIntent, ...]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT * FROM durable_runs WHERE terminal_at IS NULL ORDER BY created_at, run_id",
            ).fetchall()
            now = self._clock()
            return tuple(self._intent(dict(row), now) for row in rows)

    def cancel_reservation(self, run_id: str) -> RunSnapshot:
        with self._transaction(write=True) as db:
            row = self._get(db, run_id)
            now = self._clock()
            if row["dispatch"] == DispatchState.RESERVED:
                self._close_reservation(db, row, now, [])
            return self._snapshot(row, now)

    def note_expiry(self, run_id: str, *, can_cancel: bool) -> DispatchIntent | None:
        with self._transaction(write=True) as db:
            row = self._get(db, run_id)
            now = self._clock()
            if row["terminal_at"] is not None or now < row["expires_at"]:
                return None
            events = self._expiry_events(row, now)
            if row["dispatch"] == DispatchState.RESERVED:
                self._close_reservation(db, row, now, events)
                return None
            cancel = can_cancel and row["handle"] is not None and not row["expiry_cancel_requested"]
            if cancel:
                row["expiry_cancel_requested"] = 1
                events.append(("expiry_cancel_requested", {}))
            if events:
                self._record(db, row, events, now)
            if cancel:
                return self._intent(row, now)
            return None

    @staticmethod
    def _expiry_events(row: dict[str, Any], now: float) -> list[tuple[str, dict[str, JsonValue]]]:
        if row["expires_at"] <= now and not row["expiry_noted"]:
            row["expiry_noted"] = 1
            return [("expired", {})]
        return []

    def _close_reservation(
        self, db: sqlite3.Connection, row: dict[str, Any], now: float,
        events: list[tuple[str, dict[str, JsonValue]]],
    ) -> None:
        row["dispatch"] = DispatchState.CLOSED
        self._transition(row, AgentRunState.CANCELLED, None, now, events)
        self._record(db, row, events, now)

    def prune(self) -> None:
        with self._transaction(write=True) as db:
            now = self._clock()
            db.execute(
                "DELETE FROM durable_runs WHERE terminal_at <= ?",
                (now - self.retention.retry_window,),
            )
            threshold = now - self.retention.results_ttl
            db.execute(
                """
                DELETE FROM durable_run_events
                WHERE run_id IN (SELECT run_id FROM durable_runs WHERE terminal_at <= ?)
                """,
                (threshold,),
            )
            db.execute(
                "UPDATE durable_runs SET result = NULL, handle = NULL, progress = NULL "
                "WHERE terminal_at <= ?",
                (threshold,),
            )

    def legacy_frozen(self) -> bool:
        with self._transaction() as db:
            return db.execute("SELECT 1 FROM legacy_freeze").fetchone() is not None

    def freeze_legacy(self) -> None:
        with self._transaction(write=True) as db:
            db.execute("INSERT OR IGNORE INTO legacy_freeze VALUES (1)")

    def reserve_legacy(self, session_id: str) -> str:
        ticket = uuid4().hex
        with self._transaction(write=True) as db:
            if db.execute("SELECT 1 FROM legacy_freeze").fetchone() is not None:
                raise AdmissionsClosed("Legacy submissions are disabled")
            db.execute("INSERT INTO legacy_admissions VALUES (?, ?, NULL)", (ticket, session_id))
        return ticket

    def discard_rejected_legacy(self, ticket: str) -> None:
        with self._transaction(write=True) as db:
            db.execute("DELETE FROM legacy_admissions WHERE ticket = ? AND run_id IS NULL", (ticket,))

    def bind_legacy(self, ticket: str, run_id: str) -> None:
        with self._transaction(write=True) as db:
            db.execute("UPDATE legacy_admissions SET run_id = ? WHERE ticket = ?", (run_id, ticket))

    def unresolved_legacy(self) -> tuple[tuple[str, str, str | None], ...]:
        with self._transaction() as db:
            return tuple(tuple(row) for row in db.execute(
                "SELECT ticket, session_id, run_id FROM legacy_admissions ORDER BY ticket"
            ))

    def settle_legacy(self, outcome: LegacyOutcome) -> None:
        if outcome.status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Legacy settlement requires a terminal outcome")
        with self._transaction(write=True) as db:
            removed = db.execute(
                "DELETE FROM legacy_admissions WHERE session_id = ? AND run_id = ?",
                (outcome.session_id, outcome.run_id),
            )
            if removed.rowcount and db.execute("SELECT 1 FROM legacy_freeze").fetchone():
                db.execute("INSERT INTO legacy_cutover_outcomes VALUES (?, ?, ?, ?)",
                           (outcome.session_id, outcome.run_id, outcome.status, self._clock()))

    def legacy_outcomes(self) -> tuple[LegacyOutcome, ...]:
        with self._transaction() as db:
            rows = db.execute(
                "SELECT session_id, run_id, status FROM legacy_cutover_outcomes "
                "ORDER BY session_id, run_id"
            )
            return tuple(LegacyOutcome(*row) for row in rows)
