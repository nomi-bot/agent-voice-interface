"""Exercise the negotiated AVIP connection driver against the durable SQLite service."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import tempfile
import unittest

from avi.core.avip_session import AvipSession
from avi.core.durable_auth import LoopbackCredentials, RunPermission
from avi.core.durable_controls import DurableControls
from avi.core.durable_runs import DurableRunService
from avi.core.durable_store import Observation, SQLiteRunStore
from avi.core.lifecycle import AgentRunState
from avi.core.session import SessionLimits
from avi.protocols.avip import Capabilities
from test_avip_live_mount import Gate, _uplink
from test_jev28_durable_runs import Backend
from test_jev28_wire_contract import _FIXTURE


class NegotiatedControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.credentials = LoopbackCredentials()
        self.token = self.credentials.issue("alice", RunPermission)
        self.backend = Backend()
        self.service = DurableRunService(
            SQLiteRunStore(Path(directory) / "runs.sqlite"), self.credentials,
            self.backend, backend_scope="test", admissions_open=True, max_wire_submissions=1,
        )

    @asynccontextmanager
    async def socket(self):
        gate = Gate()
        controls = DurableControls(self.service, self.credentials, approve_query=lambda _, q: q == "synthetic query")
        session = AvipSession(
            gate, SessionLimits(), Capabilities(audio_input=True, audio_output=True), durable_controls=controls,
        )
        task = asyncio.create_task(session.run())
        try:
            await gate.inbox.put(("text", _FIXTURE["positive"]["start"]))
            ready = await asyncio.wait_for(gate.next_item(), 2)
            self.assertEqual(ready["capabilities"], _FIXTURE["positive"]["ready"]["capabilities"])
            yield gate
        finally:
            await gate.inbox.put(None)
            await asyncio.wait_for(task, 2)

    async def request(self, gate, operation, **fields):
        await gate.inbox.put(("text", {"protocol": "avip/1", "type": f"agent.run.{operation}", **fields}))
        return await asyncio.wait_for(gate.next_item(), 2)

    async def authenticate(self, gate, token):
        self.assertEqual(await self.request(gate, "authenticate", token=token), _FIXTURE["positive"]["authenticated"])

    async def submit(self, gate, query="synthetic query"):
        return await self.request(gate, "submit", request_id="request-1", kind="read_only", arguments={"query": query})

    async def test_fresh_connection_owner_result_and_ordered_replay(self):
        async with self.socket() as first:
            self.assertEqual(await self.request(first, "result", run_id="run-1"), _FIXTURE["access_denied"]["reply"])
            await self.authenticate(first, self.token)
            run = await self.submit(first)
            self.assertEqual(run["state"], "running")
            self.assertEqual(await self.submit(first), run)
        run_id = run["run_id"]
        self.backend.observations[run_id] = Observation(
            self.backend.observations[run_id].handle, AgentRunState.COMPLETED, result="synthetic answer",
        )
        await self.service.recover(startup=False)
        async with self.socket() as second:
            await self.authenticate(second, self.credentials.issue("alice", RunPermission))
            event = await self.request(second, "attach", run_id=run_id, after_cursor=run["cursor"])
            self.assertEqual((event["kind"], event["payload"]), ("completed", {"result": "synthetic answer"}))
            receipt = await asyncio.wait_for(second.next_item(), 2)
            self.assertEqual(receipt["resume_cursor"], event["cursor"])
            answer = await self.request(second, "result", run_id=run_id)
            self.assertEqual(answer["result"], "synthetic answer")
            self.assertEqual(answer["status"]["run_id"], run_id)
        async with self.socket() as foreign:
            await self.authenticate(foreign, self.credentials.issue("bob", RunPermission))
            self.assertEqual(await self.request(foreign, "cancel", run_id=run_id), _FIXTURE["access_denied"]["reply"])
        self.assertEqual(len(self.backend.submissions), 1)
        self.assertFalse(self.backend.cancellations)

    async def test_policy_schema_and_reauthentication(self):
        async with self.socket() as gate:
            await self.authenticate(gate, self.token)
            denied = _FIXTURE["access_denied"]["reply"]
            self.assertEqual((await self.request(gate, "recent"))["code"], "unsupported_message_type")
            self.assertEqual(await self.submit(gate, query="not approved"), denied)
            self.assertEqual(await self.request(gate, "authenticate", token=self.token), denied)
            bad = await self.request(gate, "submit", kind="read_only", request_id="r", arguments={"query": "x", "extra": 1})
            self.assertEqual((bad["code"], bad["field"]), ("invalid_message", "arguments"))
            for cursor in (-1, True, 2**53):
                with self.subTest(cursor=cursor):
                    error = await self.request(gate, "progress", run_id="run-1", after_cursor=cursor)
                    self.assertEqual(error["code"], "invalid_cursor")
            self.credentials.revoke(self.token)
            self.assertEqual(await self.submit(gate), denied)
        self.assertFalse(self.backend.submissions)

    async def test_slow_admission_leaves_audio_responsive(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        submit = self.backend.submit

        async def pause(*args, **kwargs):
            entered.set()
            await release.wait()
            return await submit(*args, **kwargs)

        self.backend.submit = pause
        async with self.socket() as gate:
            await self.authenticate(gate, self.token)
            await gate.inbox.put(("text", _FIXTURE["positive"]["submit"]))
            await asyncio.wait_for(entered.wait(), 2)
            await gate.inbox.put(("binary", _uplink(bytes(640))))
            self.assertIsInstance(await asyncio.wait_for(gate.next_item(), 2), bytes)
            release.set()
            self.assertEqual((await asyncio.wait_for(gate.next_item(), 2))["state"], "running")

    async def test_disconnect_during_admission_preserves_receipt_with_bounded_inflight_work(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_submit = self.backend.submit

        async def pause(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_submit(*args, **kwargs)

        self.backend.submit = pause
        try:
            async with self.socket() as first:
                await self.authenticate(first, self.token)
                await first.inbox.put(("text", _FIXTURE["positive"]["submit"]))
                await asyncio.wait_for(entered.wait(), 2)
            async with self.socket() as second:
                await self.authenticate(second, self.token)
                blocked = await self.submit(second)
                self.assertEqual(blocked["code"], "queue_full")
                release.set()
                await asyncio.wait_for(self.service.settle_submissions(), 2)
                receipt = await self.submit(second)
                self.assertEqual(receipt["dispatch"], "accepted")
                self.assertEqual(receipt["state"], "running")
                self.assertEqual(len(self.backend.submissions), 1)
        finally:
            release.set()
            await self.service.settle_submissions()
