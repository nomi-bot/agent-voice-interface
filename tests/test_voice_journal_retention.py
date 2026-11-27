"""Prune private call metadata only after a closed session's retry window."""
from pathlib import Path
import tempfile
import unittest

from avi.core.voice_tool_journal import VoiceCall, VoiceToolJournal


class JournalRetentionTests(unittest.TestCase):
    def test_closed_and_abandoned_sessions_expire_without_pruning_active_calls(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        now = 0
        journal = VoiceToolJournal(root / "voice.sqlite", retry_window=60, clock=lambda: now)
        def begin(session):
            journal.begin(VoiceCall(session, "turn", "response", "call", "request"), "hash")
        begin("abandoned")
        now = 100
        journal.expire_abandoned_sessions()
        journal.close_session("closed")
        begin("closed")  # worker finishes after detach
        begin("active")
        now = 159
        journal.prune()
        for session in ("active", "closed", "abandoned"):
            self.assertIsNotNone(journal.read(session, "call"))
        now += 1
        journal.prune()
        self.assertIsNotNone(journal.read("active", "call"))
        self.assertIsNone(journal.read("abandoned", "call"))
        self.assertIsNone(journal.read("closed", "call"))
