"""Deterministic, offline checks for mutable policy at the provider boundary."""

import asyncio
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch

from avi.core.durable_auth import AccessDenied, LoopbackCredentials, RunPermission
from avi.core.durable_runs import DurableRunService
from avi.core.durable_store import AdmissionsClosed, DispatchState, Observation, SQLiteRunStore
from avi.core.legacy_cutover import LegacyCutover
from avi.core.lifecycle import AgentRunState as State


class Provider:
    cancellation = True

    def __init__(self):
        self.submits = []
        self.stops = []

    async def submit(self, owner, *, kind, arguments, correlation_id):
        self.submits.append(correlation_id)
        return Observation("fake-" + correlation_id, State.RUNNING)

    async def cancel(self, owner, handle):
        self.stops.append(handle)
        return Observation(handle, State.CANCELLED)


class ThreadPause:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.arrived = asyncio.Event()
        self.release = threading.Event()

    def sync(self):
        self.loop.call_soon_threadsafe(self.arrived.set)
        if not self.release.wait(5):
            raise TimeoutError("Test did not release the storage step")

    async def wait(self):
        await asyncio.wait_for(self.arrived.wait(), 5)


class DurableAdmissionGatesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = SQLiteRunStore(directory / "runs.sqlite")
        self.credentials = LoopbackCredentials()
        token = self.credentials.issue("owner-a", RunPermission)
        self.owner = self.credentials.authenticate(token)
        self.provider = Provider()
        self.legacy = LegacyCutover(self.store)
        self.legacy.begin()
        self.service = DurableRunService(
            self.store, self.credentials, self.provider, backend_scope="fixture",
            admissions_open=True, legacy_cutover=self.legacy,
        )
        self.arguments = {"item": "synthetic"}

    async def submit(self, request_id):
        return await self.service.submit(self.owner, request_id, "inspect", self.arguments)

    async def assert_rollback_fences(self, storage_step):
        await self.legacy.enable_durable()
        pause = ThreadPause()
        original = getattr(self.store, storage_step)

        def at_store(*args, **kwargs):
            result = original(*args, **kwargs)
            pause.sync()
            return result

        with patch.object(self.store, storage_step, side_effect=at_store):
            task = asyncio.create_task(self.submit("new-request"))
            try:
                await pause.wait()
                await self.legacy.rollback()
            finally:
                pause.release.set()
            try:
                with self.assertRaises(AdmissionsClosed):
                    await asyncio.wait_for(task, 5)
            finally:
                self.assertEqual(self.provider.submits, [])
        # The race won the dispatch claim but could never have sent anything.
        existing = await self.submit("new-request")
        self.assertEqual((existing.dispatch, existing.state, existing.failure_code), (
            DispatchState.CLOSED, State.FAILED, "dispatch_denied",
        ))
        await self.legacy.enable_durable()
        self.assertEqual((await self.submit("new-request")).run_id, existing.run_id)
        self.assertEqual(self.provider.submits, [])

    async def test_rollback_during_reserve_never_dispatches(self):
        await self.assert_rollback_fences("reserve")

    async def test_rollback_after_persisted_claim_never_dispatches(self):
        await self.assert_rollback_fences("claim_dispatch")

    async def test_revocation_during_last_cancel_lookup_never_stops_provider(self):
        await self.legacy.enable_durable()
        token = self.credentials.issue("owner-a", [RunPermission.CANCEL])
        cancelling_owner = self.credentials.authenticate(token)
        reserved = self.store.reserve(
            "owner-a", "existing", "inspect", self.arguments, allow_new=True, backend_scope="fixture",
        )
        original_cancel = self.store.cancel_reservation
        original_intent = self.store.intent
        prior_cancel = ThreadPause()
        final_lookup = ThreadPause()
        reads = 0

        def before_cancel(run_id):
            prior_cancel.sync()
            return original_cancel(run_id)

        def gated_intent(owner, run_id):
            nonlocal reads
            intent = original_intent(owner, run_id)
            reads += 1
            if reads == 2:
                final_lookup.sync()
            return intent

        with (
            patch.object(self.store, "intent", side_effect=gated_intent),
            patch.object(self.store, "cancel_reservation", side_effect=before_cancel),
        ):
            task = asyncio.create_task(self.service.cancel(cancelling_owner, reserved.run_id))
            try:
                await prior_cancel.wait()
                admitted = await self.submit("existing")
                self.assertEqual(admitted.run_id, reserved.run_id)
                self.assertEqual(admitted.dispatch, DispatchState.ACCEPTED)
                prior_cancel.release.set()
                await final_lookup.wait()
                self.credentials.revoke(token)
            finally:
                prior_cancel.release.set()
                final_lookup.release.set()
            with self.assertRaises(AccessDenied):
                await asyncio.wait_for(task, 5)
        self.assertEqual(self.provider.submits, [reserved.run_id])
        self.assertEqual(self.provider.stops, [])
        self.assertEqual((await self.service.status(self.owner, reserved.run_id)).state, State.RUNNING)


if __name__ == "__main__":
    unittest.main()
