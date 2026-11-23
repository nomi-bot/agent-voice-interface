"""Fault injection across independent voice and durable SQLite transactions."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from avi.core.durable_auth import LoopbackCredentials, RunPermission
from avi.core.durable_runs import DurableRunService
from avi.core.durable_store import AdmissionsClosed, DispatchState, Observation, RunConflict, SQLiteRunStore
from avi.core.events import VoiceEvent
from avi.core.lifecycle import AgentRunState
from avi.core.voice_delegation import DELEGATE_TOOL, VoiceToolSession, _digest, _parse_tool
from avi.core.voice_tool_journal import VoiceCall, VoiceToolJournal


class CaptureProvider:
    def __init__(self):
        self.replies = []

    async def tool_result(self, call_id, result):
        self.replies.append((call_id, result))


class FakeBackend:
    cancellation = False

    def __init__(self):
        self.submissions = []

    async def submit(self, owner, *, kind, arguments, correlation_id):
        self.submissions.append((owner, correlation_id))
        return Observation("handle-" + correlation_id, AgentRunState.RUNNING)

    async def reconcile(self, owner, *, correlation_id, handle):
        raise AssertionError("Recovery binding must not call Hermes")

    async def cancel(self, owner, handle):
        raise AssertionError("Recovery must not cancel")


class SimulatedCrash(RuntimeError):
    pass


class VoiceJournalRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = directory / "runs.sqlite"
        self.journal_file = directory / "voice.sqlite"
        self.store = SQLiteRunStore(self.db)
        self.journal = VoiceToolJournal(self.journal_file)
        self.credentials = LoopbackCredentials()
        self.alice = self.credentials.authenticate(self.credentials.issue("alice", RunPermission))
        self.bob = self.credentials.authenticate(self.credentials.issue("bob", RunPermission))
        self.backend = FakeBackend()
        self.provider = CaptureProvider()
        self.arguments = {"query": "Return the fixed fixture marker."}
        self.event = VoiceEvent("tool_call", {
            "id": "provider-call", "name": DELEGATE_TOOL, "args": self.arguments,
        })
        self.call = VoiceCall(
            "trusted-voice-session", "turn-original", "response-original", "provider-call",
            "voice-" + _digest(["trusted-voice-session", "provider-call"]),
        )

    def service(self, scope="fixture-profile"):
        return DurableRunService(
            SQLiteRunStore(self.db), self.credentials, self.backend,
            backend_scope=scope, admissions_open=True,
        )

    def session(self, *, owner=None, scope="fixture-profile"):
        return VoiceToolSession(
            session_id=self.call.session_id, connection=self.alice if owner is None else owner,
            provider=self.provider, runs=self.service(scope), approve_query=lambda owner, query: True,
            journal=VoiceToolJournal(self.journal_file), run_conflict=RunConflict,
            admissions_closed=AdmissionsClosed,
        )

    async def admit(self, arguments=None):
        return await self.service().submit(
            self.alice, self.call.request_id, "read_only", self.arguments if arguments is None else arguments,
        )

    async def replay(self, **options):
        await self.session(**options).accept(self.event, turn_id="turn-later", response_id="response-later")

    def pending_row(self):
        self.journal.begin(self.call, _parse_tool(self.event.payload).fingerprint)

    def delivery(self):
        return self.journal.read(self.call.session_id, self.call.call_id)

    def assert_unbound_and_silent(self):
        self.assertIsNone(self.delivery().run_id)
        self.assertEqual(self.provider.replies, [])

    async def test_post_admission_crash_binds_original_once_without_reply(self):
        with patch.object(VoiceToolJournal, "bind_run", side_effect=SimulatedCrash):
            with self.assertRaises(SimulatedCrash):
                await self.session().accept(
                    self.event, turn_id=self.call.turn_id, response_id=self.call.response_id,
                )
        self.assertEqual(self.delivery().delivery, "pending")
        self.assert_unbound_and_silent()
        self.assertEqual(len(self.backend.submissions), 1)
        stored = self.backend.submissions[0]
        self.assertEqual(stored[0], "alice")
        await self.replay()
        self.assertEqual(self.delivery().run_id, stored[1])
        self.assertEqual(self.delivery().turn_id, self.call.turn_id)
        self.assertEqual(self.backend.submissions, [stored])
        self.assertEqual(self.provider.replies, [])
        with patch.object(SQLiteRunStore, "lookup_admitted", create=True,
                          side_effect=AssertionError("bound calls should not look up again")):
            await self.replay()
        self.assertEqual(self.provider.replies, [])

    async def test_journal_only_does_not_admit(self):
        self.pending_row()
        await self.replay()
        self.assert_unbound_and_silent()
        self.assertEqual(self.backend.submissions, [])
        self.assertEqual(self.store.pending(), ())

    async def test_reserved_intent_does_not_dispatch(self):
        self.pending_row()
        run = self.store.reserve(
            "alice", self.call.request_id, "read_only", self.arguments,
            allow_new=True, backend_scope="fixture-profile",
        )
        await self.replay()
        self.assert_unbound_and_silent()
        self.assertEqual(self.backend.submissions, [])
        self.assertEqual((await self.service().status(self.alice, run.run_id)).dispatch, DispatchState.RESERVED)

    async def test_unknown_no_handle_remains_unbound(self):
        self.pending_row()
        with patch.object(self.backend, "submit", side_effect=TimeoutError) as submit:
            run = await self.admit()
            self.assertEqual(run.dispatch, DispatchState.UNCERTAIN)
            await self.replay()
            self.assertEqual(submit.await_count, 1)
        self.assert_unbound_and_silent()

    async def test_owner_and_status_permission_gate_recovery(self):
        self.pending_row()
        admitted = await self.admit()
        submit_only = self.credentials.authenticate(self.credentials.issue("alice", [RunPermission.SUBMIT]))
        await self.replay(owner=self.bob)
        self.assert_unbound_and_silent()
        await self.replay(owner=submit_only)
        self.assert_unbound_and_silent()
        with patch.object(SQLiteRunStore, "pending", side_effect=AssertionError("must not scan")):
            await self.replay()
        self.assertEqual(self.delivery().run_id, admitted.run_id)
        self.assertEqual(len(self.backend.submissions), 1)
        self.assertEqual(self.provider.replies, [])

    async def test_changed_digest_stays_silent(self):
        self.pending_row()
        await self.admit({"query": "Different immutable sample task."})
        await self.replay()
        self.assert_unbound_and_silent()
        self.assertEqual(len(self.backend.submissions), 1)

    async def test_changed_backend_scope_stays_silent(self):
        self.pending_row()
        await self.admit()
        await self.replay(scope="changed-profile")
        self.assert_unbound_and_silent()
        self.assertEqual(len(self.backend.submissions), 1)


if __name__ == "__main__":
    unittest.main()
