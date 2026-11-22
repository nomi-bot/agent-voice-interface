"""Server-side correlations and reply attempts for voice tool calls."""

from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3


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

    def __init__(self, path: Path):
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

    def begin(self, call: VoiceCall, fingerprint: str) -> bool:
        with closing(sqlite3.connect(self._path)) as db, db:
            db.execute(
                """INSERT INTO voice_tool_deliveries
                   (session_id, turn_id, response_id, call_id, request_id, fingerprint)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, call_id) DO NOTHING""",
                (call.session_id, call.turn_id, call.response_id, call.call_id, call.request_id, fingerprint),
            )
            saved_fingerprint, delivery = db.execute(
                "SELECT fingerprint, delivery FROM voice_tool_deliveries WHERE session_id = ? AND call_id = ?",
                (call.session_id, call.call_id),
            ).fetchone()
            if saved_fingerprint != fingerprint:
                raise VoiceCallConflict("Voice tool call payload changed")
            return delivery == "pending"

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
