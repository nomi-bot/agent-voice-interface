import asyncio
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from textwrap import dedent
import unittest

from avi.agents.base import AgentCapabilities, Run
from avi.agents.mock import MockAgentAdapter
from avi.core.durable_auth import LoopbackCredentials, RunPermission
from avi.core.durable_runs import DurableRunService
from avi.core.durable_store import AdmissionsClosed, SQLiteRunStore
from avi.core.legacy_cutover import CutoverBlocked, LegacyCutover
from avi.core.orchestrator import VoiceOrchestrator
from test_jev28_durable_runs import Backend


class PausedLegacyAdapter:
    capabilities = AgentCapabilities(status=True, cancellation=True)

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.run = Run("legacy-run", "running")
        self.cancellation_confirmed = False

    async def submit(self, request):
        self.entered.set()
        await self.release.wait()
        return self.run

    async def status(self, run_id):
        return self.run

    async def cancel(self, run_id):
        if self.cancellation_confirmed:
            self.run = Run(run_id, "cancelled")
        return self.run

    async def close(self):
        pass


class LegacyCutoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "runs.db"
        self.store = SQLiteRunStore(self.path)
        self.cutover = LegacyCutover(self.store)
        self.credentials = LoopbackCredentials()
        self.connection = self.credentials.authenticate(
            self.credentials.issue("alice", RunPermission),
        )
        self.service = DurableRunService(
            self.store, self.credentials, Backend(),
            admissions_open=True, backend_scope="fake", legacy_cutover=self.cutover,
        )

    async def durable_submit(self, request_id):
        return await self.service.submit(self.connection, request_id, "read_only", {"query": "fixture"})

    async def test_cutover_freezes_legacy_until_pending_and_unconfirmed_runs_are_terminal(self):
        legacy_agent = PausedLegacyAdapter()
        tracked_agent = self.cutover.track(legacy_agent)
        legacy = VoiceOrchestrator(tracked_agent)
        submitted = asyncio.create_task(legacy.handle({"type": "submit", "request": "fixture"}))
        await legacy_agent.entered.wait()
        self.cutover.begin()
        with self.assertRaises(AdmissionsClosed):
            await legacy.handle({"type": "submit", "request": "new legacy task"})
        with self.assertRaises(CutoverBlocked):
            await self.cutover.enable_durable()
        with self.assertRaises(AdmissionsClosed):
            await self.durable_submit("first")
        legacy_agent.release.set()
        await submitted
        with self.assertRaises(CutoverBlocked):
            await self.cutover.enable_durable(cancel=True)
        self.assertEqual(self.store.legacy_outcomes(), ())

        legacy_agent.cancellation_confirmed = True
        await self.cutover.enable_durable(cancel=True)
        recorded = SQLiteRunStore(self.path).legacy_outcomes()
        self.assertEqual([(outcome.run_id, outcome.status) for outcome in recorded], [
            ("legacy-run", "cancelled"),
        ])
        accepted = await self.durable_submit("first")
        self.assertEqual((await self.service.status(self.connection, accepted.run_id)).run_id, accepted.run_id)

    async def test_terminal_legacy_outcome_can_be_drained_without_cancelling(self):
        agent = PausedLegacyAdapter()
        agent.release.set()
        tracked = self.cutover.track(agent)
        run = await tracked.submit("fixture")
        self.cutover.begin()
        agent.run = Run(run.id, "completed", "example result")
        await self.cutover.enable_durable()
        outcomes = SQLiteRunStore(self.path).legacy_outcomes()
        self.assertEqual([outcome.status for outcome in outcomes], ["completed"])
        self.assertNotIn("example result", repr(outcomes))

    async def test_rollback_keeps_legacy_frozen_and_existing_durable_run_readable(self):
        legacy = self.cutover.track(MockAgentAdapter(delay=10))
        await legacy.submit("fixture")
        self.cutover.begin()
        await self.cutover.enable_durable(cancel=True)
        durable = await self.durable_submit("first")
        await self.cutover.rollback()
        with self.assertRaises(AdmissionsClosed):
            await self.durable_submit("new")
        with self.assertRaises(AdmissionsClosed):
            await legacy.submit("legacy again")
        self.assertEqual((await self.durable_submit("first")).run_id, durable.run_id)
        self.assertEqual((await self.service.status(self.connection, durable.run_id)).run_id, durable.run_id)
        await legacy.close()

    async def test_process_exit_with_unknown_or_known_handle_blocks_cutover(self):
        for stage in ("in_submit", "after_acceptance"):
            with self.subTest(stage=stage):
                path = self.path.with_name(stage + ".db")
                # Terminate abruptly: no Python task cleanup or finalizer can settle the ledger.
                child = dedent("""
                    import asyncio, os, sys
                    from pathlib import Path
                    from avi.agents.mock import MockAgentAdapter
                    from avi.core.durable_store import SQLiteRunStore
                    from avi.core.legacy_cutover import LegacyCutover

                    class Adapter(MockAgentAdapter):
                        async def submit(self, request):
                            if sys.argv[2] == "in_submit":
                                os._exit(42)
                            return await super().submit(request)

                    async def main():
                        gate = LegacyCutover(SQLiteRunStore(Path(sys.argv[1])))
                        await gate.track(Adapter(delay=99)).submit("private fixture")
                        gate.begin()
                        os._exit(42)

                    asyncio.run(main())
                """)
                status = subprocess.run([sys.executable, "-c", child, str(path), stage],
                                        timeout=10, capture_output=True)
                self.assertEqual(status.returncode, 42, status.stderr)
                store = SQLiteRunStore(path)
                recovered = LegacyCutover(store)
                if stage == "after_acceptance":
                    with self.assertRaises(AdmissionsClosed):
                        await recovered.track(MockAgentAdapter()).submit("new")
                recovered.begin()
                with self.assertRaises(CutoverBlocked):
                    await recovered.enable_durable(cancel=True)
                [(ticket, session, handle)] = store.unresolved_legacy()
                self.assertTrue(ticket)
                self.assertNotIn(b"private fixture", path.read_bytes())
                if stage == "in_submit":
                    self.assertIsNone(handle)
                else:
                    adapter = PausedLegacyAdapter()
                    adapter.run = Run("different-handle", "completed")
                    self.assertFalse(await recovered.reconcile_known_run(session, handle, adapter))
                    adapter.run = Run(handle, "running")
                    self.assertFalse(await recovered.reconcile_known_run(session, handle, adapter))
                    adapter.run = Run(handle, "completed")
                    self.assertTrue(await recovered.reconcile_known_run(session, handle, adapter))
                    await recovered.enable_durable()
                    self.assertEqual(store.legacy_outcomes()[0].status, "completed")

    async def test_only_confirmed_local_rejections_release_the_ticket(self):
        bounded = self.cutover.track(MockAgentAdapter())
        with self.assertRaises(ValueError):
            await bounded.submit(" ")
        self.assertFalse(self.store.unresolved_legacy())

        class UncertainAdapter(PausedLegacyAdapter):
            async def submit(self, request):
                raise ValueError("could not decode remote receipt")

        with self.assertRaises(ValueError):
            await self.cutover.track(UncertainAdapter()).submit("fixture")
        self.cutover.begin()
        with self.assertRaises(CutoverBlocked):
            await self.cutover.enable_durable()

    async def test_v1_database_upgrades_and_preserves_cutover_receipts(self):
        # Build a v1 fixture by removing the two additive v2 tables.
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE legacy_freeze")
            db.execute("DROP TABLE legacy_admissions")
            db.execute("PRAGMA user_version = 1")
            db.execute("INSERT INTO legacy_cutover_outcomes VALUES ('session','run','failed',1)")
        store = SQLiteRunStore(self.path)
        gate = LegacyCutover(store)
        with self.assertRaises(AdmissionsClosed):
            await gate.track(MockAgentAdapter()).submit("new")
        await gate.enable_durable()
        self.assertEqual(store.legacy_outcomes()[0].status, "failed")

    async def test_disconnect_keeps_the_original_mock_cleanup_policy(self):
        legacy = self.cutover.track(MockAgentAdapter(delay=10))
        run = await legacy.submit("fixture")
        self.cutover.begin()
        await legacy.close()
        await self.cutover.enable_durable()
        self.assertEqual([(outcome.run_id, outcome.status) for outcome in self.store.legacy_outcomes()], [
            (run.id, "cancelled"),
        ])
