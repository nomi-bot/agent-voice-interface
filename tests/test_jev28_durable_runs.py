import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from avi.core.durable_auth import AccessDenied, LoopbackCredentials, RunPermission
from avi.core.durable_runs import DispatchError, DurableRunService, Unsupported
from avi.core.durable_store import (
    AdmissionsClosed,
    DispatchState,
    InvalidCursor,
    Observation,
    Retention,
    RunConflict,
    SQLiteRunStore,
)
from avi.core.lifecycle import AgentRunState


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Backend:
    cancellation = True

    def __init__(self):
        self.submissions = []
        self.cancellations = []
        self.reconciliations = []
        self.observations = {}
        self.lose_submit_response = False
        self.finish_cancel = True

    async def submit(self, owner, *, kind, arguments, correlation_id):
        self.submissions.append((owner, kind, arguments, correlation_id))
        observed = Observation(f"handle-{correlation_id}", AgentRunState.RUNNING)
        self.observations[correlation_id] = observed
        if self.lose_submit_response:
            raise DispatchError("uncertain")
        return observed

    async def reconcile(self, owner, *, correlation_id, handle):
        self.reconciliations.append((owner, correlation_id, handle))
        return self.observations.get(correlation_id)

    async def cancel(self, owner, handle):
        self.cancellations.append((owner, handle))
        if self.finish_cancel:
            return Observation(handle, AgentRunState.CANCELLED)
        return None


class DurableRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "runs.db"
        self.clock = Clock()
        self.credentials = LoopbackCredentials(clock=self.clock)
        self.alice_token = self.credentials.issue("alice", RunPermission)
        self.alice = self.credentials.authenticate(self.alice_token)
        self.bob = self.credentials.authenticate(self.credentials.issue("bob", RunPermission))
        self.backend = Backend()
        self.retention = Retention(execution_ttl=120, results_ttl=30, retry_window=120)
        self.service = self.reopen()

    def reopen(self, *, admissions_open=True, backend_scope="test-profile-1"):
        store = SQLiteRunStore(self.path, retention=self.retention, clock=self.clock)
        return DurableRunService(
            store, self.credentials, self.backend, admissions_open=admissions_open,
            backend_scope=backend_scope,
        )

    async def submit(self, request_id="request-1"):
        return await self.service.submit(self.alice, request_id, "inspect", {"item": "synthetic"})

    async def test_concurrent_identical_submits_dispatch_once_across_store_connections(self):
        other_service = self.reopen()
        first, second = await asyncio.gather(
            self.service.submit(self.alice, "same", "inspect", {"a": 1, "b": "x"}),
            other_service.submit(self.alice, "same", "inspect", {"b": "x", "a": 1}),
        )
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(len(self.backend.submissions), 1)
        self.assertEqual(self.backend.submissions[0][3], first.run_id)

    async def test_concurrent_conflicting_submits_leave_one_intent(self):
        replies = await asyncio.gather(
            self.service.submit(self.alice, "same", "inspect", {"item": 1}),
            self.reopen().submit(self.alice, "same", "inspect", {"item": 2}),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(reply, RunConflict) for reply in replies), 1)
        self.assertEqual(len(self.backend.submissions), 1)

    async def test_distinct_authenticated_owners_can_use_the_same_request_id(self):
        first = await self.submit()
        second = await self.service.submit(self.bob, "request-1", "inspect", {"item": "synthetic"})
        self.assertNotEqual(first.run_id, second.run_id)

    async def test_cross_owner_and_guessed_id_are_indistinguishable_for_each_read_and_cancel(self):
        run = await self.submit()
        for method in (
            self.service.status, self.service.result, self.service.attach,
            self.service.progress, self.service.cancel,
        ):
            with self.subTest(operation=method.__name__):
                with self.assertRaises(AccessDenied) as foreign:
                    await method(self.bob, run.run_id)
                with self.assertRaises(AccessDenied) as unknown:
                    await method(self.bob, "unknown-run")
                self.assertEqual(str(foreign.exception), str(unknown.exception))
        self.assertEqual(self.backend.cancellations, [])

    async def test_permissions_are_checked_per_operation_and_again_after_revocation(self):
        run = await self.submit()
        reader = self.credentials.authenticate(self.credentials.issue("alice", [RunPermission.STATUS]))
        self.assertEqual((await self.service.status(reader, run.run_id)).state, AgentRunState.RUNNING)
        for method in (self.service.result, self.service.attach, self.service.progress, self.service.cancel):
            with self.subTest(operation=method.__name__), self.assertRaises(AccessDenied):
                await method(reader, run.run_id)
        self.credentials.revoke(self.alice_token)
        for method in (
            self.service.status, self.service.result, self.service.attach,
            self.service.progress, self.service.cancel,
        ):
            with self.subTest(operation=method.__name__), self.assertRaises(AccessDenied):
                await method(self.alice, run.run_id)
        with self.assertRaises(AccessDenied):
            await self.submit("another")

    async def test_revocation_during_lookup_blocks_terminal_cancel_and_duplicate_submit_replies(self):
        run = await self.submit()
        await self.service.cancel(self.alice, run.run_id)
        real_lookup = SQLiteRunStore.intent

        def revoke_on_lookup(store, owner, run_id):
            intent = real_lookup(store, owner, run_id)
            self.credentials.revoke(self.alice_token)
            return intent

        for operation in ("cancel", "submit"):
            self.alice_token = self.credentials.issue("alice", RunPermission)
            self.alice = self.credentials.authenticate(self.alice_token)
            with self.subTest(operation=operation), patch.object(SQLiteRunStore, "intent", revoke_on_lookup):
                with self.assertRaises(AccessDenied):
                    if operation == "submit":
                        await self.submit()
                    else:
                        await self.service.cancel(self.alice, run.run_id)
        self.assertEqual(len(self.backend.submissions), 1)
        self.assertEqual(len(self.backend.cancellations), 1)

    async def test_revocation_during_backend_submit_preserves_the_accepted_run(self):
        original_submit = self.backend.submit

        async def revoke_on_submit(*args, **kwargs):
            observed = await original_submit(*args, **kwargs)
            self.credentials.revoke(self.alice_token)
            return observed

        self.backend.submit = revoke_on_submit
        with self.assertRaises(AccessDenied):
            await self.submit()
        self.alice = self.credentials.authenticate(self.credentials.issue("alice", RunPermission))
        result = await self.submit()
        self.assertEqual(result.state, AgentRunState.RUNNING)
        self.assertEqual(len(self.backend.submissions), 1)

    async def test_attach_without_progress_and_result_grants_cannot_read_replay(self):
        run = await self.submit()
        attach_only = self.credentials.authenticate(
            self.credentials.issue("alice", [RunPermission.ATTACH]),
        )
        with self.assertRaises(AccessDenied):
            await self.service.attach(attach_only, run.run_id)

    async def test_malformed_run_id_does_not_reveal_service_details(self):
        with self.assertRaises(AccessDenied):
            await self.service.status(self.alice, {"run_id": "forged"})

    async def test_expired_connection_and_forged_token_deny_durable_access(self):
        short_token = self.credentials.issue("alice", RunPermission, lifetime=1)
        short_connection = self.credentials.authenticate(short_token)
        run = await self.submit()
        self.clock.now += 1
        with self.assertRaises(AccessDenied):
            self.credentials.authenticate("a" * 43)
        for method in (
            self.service.status, self.service.result, self.service.attach,
            self.service.progress, self.service.cancel,
        ):
            with self.subTest(operation=method.__name__), self.assertRaises(AccessDenied):
                await method(short_connection, run.run_id)
        with self.assertRaises(AccessDenied):
            await self.service.submit(short_connection, "new", "inspect", {})

    async def test_provider_admission_persists_handle_without_claiming_started_work(self):
        async def accept(owner, *, kind, arguments, correlation_id):
            admitted = Observation("admitted-handle", AgentRunState.SUBMITTED)
            self.backend.observations[correlation_id] = admitted
            return admitted

        self.backend.submit = accept
        admitted = await self.submit()
        self.assertEqual(admitted.dispatch, DispatchState.ACCEPTED)
        self.assertEqual(admitted.state, AgentRunState.SUBMITTED)
        self.backend.observations[admitted.run_id] = Observation(
            "admitted-handle", AgentRunState.RUNNING,
        )
        restarted = self.reopen()
        await restarted.recover()
        self.assertEqual(self.backend.reconciliations, [("alice", admitted.run_id, "admitted-handle")])
        self.assertEqual((await restarted.status(self.alice, admitted.run_id)).state, AgentRunState.RUNNING)

    async def test_lost_acceptance_is_reconciled_after_reopen_without_resubmission(self):
        self.backend.lose_submit_response = True
        uncertain = await self.submit()
        self.assertEqual(uncertain.dispatch, DispatchState.UNCERTAIN)
        self.assertEqual(uncertain.state, AgentRunState.SUBMITTED)
        recovered = self.reopen()
        self.backend.observations[uncertain.run_id] = Observation(
            f"handle-{uncertain.run_id}", AgentRunState.COMPLETED, result="synthetic answer",
        )
        await recovered.recover()
        await recovered.recover()
        result = await recovered.result(self.alice, uncertain.run_id)
        self.assertEqual(result.result, "synthetic answer")
        replay = await recovered.attach(self.alice, uncertain.run_id)
        self.assertEqual([e.cursor for e in replay.events], list(range(1, replay.resume_cursor + 1)))
        self.assertEqual(sum(e.kind == "completed" for e in replay.events), 1)
        self.assertEqual(len(self.backend.submissions), 1)

    async def test_rotated_provider_scope_preserves_old_run_without_query_or_cancel(self):
        run = await self.submit()
        rotated = self.reopen(backend_scope="test-profile-2")
        await rotated.recover()
        observed = await rotated.status(self.alice, run.run_id)
        self.assertEqual(observed.state, AgentRunState.RUNNING)
        self.assertTrue(observed.reconciliation_pending)
        self.assertEqual(self.backend.reconciliations, [])
        with self.assertRaises(Unsupported):
            await rotated.cancel(self.alice, run.run_id)
        self.clock.now = run.expires_at
        await rotated.maintain()
        self.assertEqual(self.backend.cancellations, [])

    async def test_missing_lookup_marks_pending_until_confirmed_evidence_returns(self):
        run = await self.submit()
        accepted = self.backend.observations.pop(run.run_id)
        await self.service.recover()
        await self.service.recover()
        pending = await self.service.status(self.alice, run.run_id)
        self.assertEqual(pending.state, AgentRunState.RUNNING)
        self.assertTrue(pending.reconciliation_pending)
        history = await self.service.attach(self.alice, run.run_id)
        self.assertEqual(sum(event.kind == "reconciliation_pending" for event in history.events), 1)
        self.backend.observations[run.run_id] = accepted
        await self.service.recover()
        self.assertFalse((await self.service.status(self.alice, run.run_id)).reconciliation_pending)

    async def test_process_exit_between_dispatch_claim_and_send_keeps_outcome_uncertain(self):
        child = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", """
import os
from pathlib import Path
import sys

from avi.core.durable_store import SQLiteRunStore

store = SQLiteRunStore(Path(sys.argv[1]), clock=lambda: 100.0)
reserved = store.reserve(
    "alice", "request-1", "inspect", {"item": "synthetic"},
    allow_new=True, backend_scope="test-profile-1",
)
if not store.claim_dispatch(reserved.run_id, backend_scope="test-profile-1"):
    raise AssertionError("submission was never claimed")
os._exit(71)
""", str(self.path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 71, child.stderr)
        self.service = self.reopen()
        await self.service.recover()
        observed = await self.submit()
        self.assertEqual(observed.dispatch, DispatchState.UNCERTAIN)
        self.assertEqual(self.backend.submissions, [])
        self.assertEqual(self.backend.reconciliations, [("alice", observed.run_id, None)])

    async def test_retry_can_dispatch_a_reservation_that_was_never_claimed(self):
        store = SQLiteRunStore(self.path, retention=self.retention, clock=self.clock)
        reserved = store.reserve(
            "alice", "request-1", "inspect", {"item": "synthetic"},
            allow_new=True, backend_scope="test-profile-1",
        )
        self.service = self.reopen()
        await self.service.recover()
        self.assertEqual(self.backend.submissions, [])
        observed = await self.submit()
        self.assertEqual(observed.run_id, reserved.run_id)
        self.assertEqual(observed.state, AgentRunState.RUNNING)
        self.assertEqual(len(self.backend.submissions), 1)

    async def test_fresh_authenticated_session_replays_result_after_delivery_disruption(self):
        run = await self.submit()
        initial = await self.service.attach(self.alice, run.run_id)
        self.clock.now += 65
        self.backend.observations[run.run_id] = Observation(
            f"handle-{run.run_id}", AgentRunState.RUNNING, progress="still running",
        )
        self.service = self.reopen()
        await self.service.recover()
        new_connection = self.credentials.authenticate(self.credentials.issue("alice", RunPermission))
        during = await self.service.attach(new_connection, run.run_id, after_cursor=initial.resume_cursor)
        self.assertEqual([event.kind for event in during.events], ["progress"])
        self.backend.observations[run.run_id] = Observation(
            f"handle-{run.run_id}", AgentRunState.COMPLETED, result="done",
        )
        await self.service.recover()
        first = await self.service.attach(new_connection, run.run_id, after_cursor=during.resume_cursor)
        again = await self.reopen().attach(new_connection, run.run_id, after_cursor=during.resume_cursor)
        self.assertEqual(first, again)
        self.assertEqual(first.events[-1].payload["result"], "done")
        self.assertEqual(self.backend.cancellations, [])

    async def test_pruned_history_returns_retained_snapshot_and_consistent_resume_cursor(self):
        self.retention = Retention(history_limit=2)
        self.service = self.reopen()
        run = await self.submit()
        self.backend.observations[run.run_id] = Observation(
            f"handle-{run.run_id}", AgentRunState.COMPLETED, result="done",
        )
        await self.service.recover()
        replay = await self.service.attach(self.alice, run.run_id)
        self.assertEqual(replay.events, ())
        self.assertEqual(replay.snapshot.result, "done")
        self.assertEqual(replay.snapshot.status.cursor, replay.resume_cursor)
        resumed = await self.service.attach(self.alice, run.run_id, after_cursor=replay.resume_cursor)
        self.assertEqual(resumed.events, ())
        self.assertIsNone(resumed.snapshot)
        with self.assertRaises(InvalidCursor):
            await self.service.attach(self.alice, run.run_id, after_cursor=replay.resume_cursor + 1)

    async def test_replay_cursor_is_consistent_with_a_concurrent_terminal_write(self):
        self.retention = Retention(history_limit=1)
        self.service = self.reopen()
        run = await self.submit()
        self.backend.observations[run.run_id] = Observation(
            f"handle-{run.run_id}", AgentRunState.COMPLETED, result="done",
        )
        replay, _ = await asyncio.gather(
            self.service.attach(self.alice, run.run_id), self.reopen().recover(),
        )
        self.assertEqual(replay.snapshot.status.cursor, replay.resume_cursor)
        next_reply = await self.service.attach(
            self.alice, run.run_id, after_cursor=replay.resume_cursor,
        )
        if replay.snapshot.status.state is AgentRunState.RUNNING:
            self.assertEqual([event.kind for event in next_reply.events], ["completed"])
        else:
            self.assertEqual(replay.snapshot.result, "done")
            self.assertEqual(next_reply.events, ())

    async def test_progress_permission_cannot_read_terminal_result(self):
        run = await self.submit()
        self.backend.observations[run.run_id] = Observation(
            f"handle-{run.run_id}", AgentRunState.COMPLETED, result="private",
        )
        await self.service.recover()
        progress_reader = self.credentials.authenticate(
            self.credentials.issue("alice", [RunPermission.PROGRESS]),
        )
        replay = await self.service.progress(progress_reader, run.run_id)
        self.assertNotIn("private", repr(replay))
        self.assertEqual(replay.events[-1].kind, "completed")
        with self.assertRaises(AccessDenied):
            await self.service.attach(progress_reader, run.run_id)

    async def test_cancellation_requires_verified_terminal_observation(self):
        run = await self.submit()
        self.backend.finish_cancel = False
        self.assertEqual((await self.service.cancel(self.alice, run.run_id)).state, AgentRunState.RUNNING)
        self.backend.finish_cancel = True
        self.assertEqual((await self.service.cancel(self.alice, run.run_id)).state, AgentRunState.CANCELLED)
        await self.service.recover()
        self.assertEqual((await self.service.status(self.alice, run.run_id)).state, AgentRunState.CANCELLED)

    async def test_expiry_attempts_verified_cancel_once_and_preserves_active_outcome(self):
        run = await self.submit()
        self.backend.finish_cancel = False
        self.clock.now = run.expires_at
        await self.service.maintain()
        await self.reopen().maintain()
        status = await self.service.status(self.alice, run.run_id)
        self.assertTrue(status.expired)
        self.assertEqual(status.state, AgentRunState.RUNNING)
        self.assertEqual(len(self.backend.cancellations), 1)

    async def test_late_terminal_observation_retains_expiry_evidence(self):
        run = await self.submit()
        self.clock.now = run.expires_at + 1
        self.backend.observations[run.run_id] = Observation(
            f"handle-{run.run_id}", AgentRunState.COMPLETED, result="done",
        )
        await self.service.recover()
        result = await self.service.result(self.alice, run.run_id)
        self.assertTrue(result.status.expired)
        self.assertEqual(result.status.state, AgentRunState.COMPLETED)
        replay = await self.service.attach(self.alice, run.run_id)
        self.assertEqual([event.kind for event in replay.events[-2:]], ["expired", "completed"])

    async def test_observed_result_must_fit_the_documented_wire_bound(self):
        with self.assertRaisesRegex(ValueError, "Invalid provider field"):
            Observation("handle", AgentRunState.COMPLETED, result="\x00" * 4096)

    async def test_result_expiry_and_retry_window_have_separate_lifetimes(self):
        run = await self.submit()
        self.backend.observations[run.run_id] = Observation(
            f"handle-{run.run_id}", AgentRunState.COMPLETED, result="done",
        )
        await self.service.recover()
        self.clock.now += self.retention.results_ttl
        self.assertFalse((await self.service.result(self.alice, run.run_id)).retained)
        replay = await self.service.attach(self.alice, run.run_id)
        self.assertIsNone(replay.snapshot.result)
        await self.service.maintain()
        self.assertEqual((await self.submit()).run_id, run.run_id)
        self.assertEqual(len(self.backend.submissions), 1)
        self.clock.now += self.retention.retry_window - self.retention.results_ttl
        await self.service.maintain()
        with self.assertRaises(AccessDenied):
            await self.service.status(self.alice, run.run_id)
        self.assertNotEqual((await self.submit()).run_id, run.run_id)

    async def test_rollback_refuses_new_runs_without_losing_access_to_existing_runs(self):
        run = await self.submit()
        self.service = self.reopen(admissions_open=False)
        self.assertEqual((await self.submit()).run_id, run.run_id)
        self.assertEqual((await self.service.status(self.alice, run.run_id)).state, AgentRunState.RUNNING)
        with self.assertRaises(AdmissionsClosed):
            await self.submit("new")

    async def test_unsupported_cancellation_preserves_state_after_expiry(self):
        self.backend.cancellation = False
        run = await self.submit()
        with self.assertRaises(Unsupported):
            await self.service.cancel(self.alice, run.run_id)
        self.clock.now = run.expires_at
        await self.service.maintain()
        status = await self.service.status(self.alice, run.run_id)
        self.assertTrue(status.expired)
        self.assertEqual(status.state, AgentRunState.RUNNING)
        self.assertEqual(self.backend.cancellations, [])
