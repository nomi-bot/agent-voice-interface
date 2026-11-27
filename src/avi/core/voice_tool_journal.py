"""Server-side correlations and reply attempts for voice tool calls."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from math import isfinite
from time import time

from avi.core.durable_store import Retention


class VoiceCallConflict(ValueError):
    """A provider reused a call ID with different arguments in one session."""


@dataclass(frozen=True)
class VoiceCall:
    session_id: str
    turn_id: str
    response_id: str
    call_id: str
    request_id: str


@dataclass(frozen=True)
class VoiceToolDelivery:
    turn_id: str
    response_id: str
    request_id: str
    run_id: str | None
    delivery: str


class VoiceToolJournal:
    """Persist a call binding independently from the run's execution state."""

    def __init__(
        self, path: Path, *, retry_window: float = Retention().retry_window,
        clock: Callable[[], float] = time,
    ):
        if not isfinite(retry_window) or retry_window <= 0:
            raise ValueError("Journal retention must be positive and finite")
        self._retry_window = retry_window
        self._clock = clock
        self._path = path.resolve()
        os.close(os.open(self._path, os.O_CREAT | os.O_RDWR, mode=0o600))
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS voice_tool_deliveries (
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    response_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    run_id TEXT,
                    delivery TEXT NOT NULL DEFAULT 'pending',
                    PRIMARY KEY (session_id, call_id)
                )"""
            )

            db.execute(
                """CREATE TABLE IF NOT EXISTS closed_voice_sessions (
                    session_id TEXT PRIMARY KEY, closed_at REAL NOT NULL
                )"""
            )

    def close_session(self, session_id: str) -> None:
        """Keep the tombstone even if the first detached write is still settling."""
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO closed_voice_sessions VALUES (?, ?)",
                (session_id, self._clock()),
            )

    def expire_abandoned_sessions(self) -> None:
        """Single-process startup: former sockets, including pre-migration ones, are closed."""
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute(
                """INSERT OR IGNORE INTO closed_voice_sessions
                   SELECT DISTINCT session_id, ? FROM voice_tool_deliveries""",
                (self._clock(),),
            )

    def prune(self) -> None:
        cutoff = self._clock() - self._retry_window
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute(
                """DELETE FROM voice_tool_deliveries WHERE session_id IN
                   (SELECT session_id FROM closed_voice_sessions WHERE closed_at <= ?)""", (cutoff,),
            )
            db.execute("DELETE FROM closed_voice_sessions WHERE closed_at <= ?", (cutoff,))

    def begin(self, call: VoiceCall, fingerprint: str) -> tuple[bool, VoiceToolDelivery]:
        """Atomically distinguish a first delivery from an existing journal row."""
        with closing(sqlite3.connect(self._path)) as db, db:
            inserted = db.execute(
                """INSERT INTO voice_tool_deliveries
                   (session_id, turn_id, response_id, call_id, request_id, fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, call_id) DO NOTHING""",
                (call.session_id, call.turn_id, call.response_id, call.call_id, call.request_id, fingerprint),
            ).rowcount == 1
            saved_fingerprint, *record = db.execute(
                """SELECT fingerprint, turn_id, response_id, request_id, run_id, delivery
                   FROM voice_tool_deliveries WHERE session_id = ? AND call_id = ?""",
                (call.session_id, call.call_id),
            ).fetchone()
            if saved_fingerprint != fingerprint:
                raise VoiceCallConflict("Voice tool call payload changed")
            return inserted, VoiceToolDelivery(*record)

    def bind_run(self, call: VoiceCall, run_id: str) -> None:
        with closing(sqlite3.connect(self._path)) as db, db:
            updated = db.execute(
                """UPDATE voice_tool_deliveries SET run_id = ? WHERE session_id = ? AND call_id = ?
                   AND (run_id IS NULL OR run_id = ?)""",
                (run_id, call.session_id, call.call_id, run_id),
            ).rowcount
            if updated != 1:
                raise VoiceCallConflict("Voice tool call run binding changed")

    def transition(self, call: VoiceCall, previous: str, delivery: str) -> bool:
        with closing(sqlite3.connect(self._path)) as db, db:
            return db.execute(
                """UPDATE voice_tool_deliveries SET delivery = ?
                   WHERE session_id = ? AND call_id = ? AND delivery = ?""",
                (delivery, call.session_id, call.call_id, previous),
            ).rowcount == 1

    def read(self, session_id: str, call_id: str) -> VoiceToolDelivery | None:
        with closing(sqlite3.connect(self._path)) as db:
            row = db.execute(
                """SELECT turn_id, response_id, request_id, run_id, delivery
                   FROM voice_tool_deliveries WHERE session_id = ? AND call_id = ?""",
                (session_id, call_id),
            ).fetchone()
            return VoiceToolDelivery(*row) if row is not None else None
