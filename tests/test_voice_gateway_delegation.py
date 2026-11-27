"""Exercise opt-in authenticated calls on the actual ASGI route with a scripted provider."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from avi.core.durable_auth import LoopbackCredentials, RunPermission
from avi.core.durable_runs import DurableRunService
from avi.core.durable_store import Observation, SQLiteRunStore
from avi.core.events import VoiceEvent
from avi.core.lifecycle import AgentRunState
from avi.core.voice_delegation import DELEGATE_TOOL, RESULT_TOOL
from avi.core.voice_tool_journal import VoiceToolJournal
from avi.main import app
from test_avip_live_mount import FakeProvider, _start, voice_env
from test_avip_websocket import connection, reply, send


class Backend:
    cancellation = False

    def __init__(self):
        self.submissions = []

    async def submit(self, owner, *, kind, arguments, correlation_id):
        self.submissions.append((owner, correlation_id))
        return Observation("private-handle", AgentRunState.COMPLETED, result="fixture result")


class DelegatingProvider(FakeProvider):
    delegation_enabled = False

    def enable_delegation_tools(self):
        assert not self.started
        self.delegation_enabled = True

    async def invoke(self, call_id, tool, arguments):
        await self.emit(VoiceEvent("tool_call", {"id": call_id, "name": tool, "args": arguments}))
        await asyncio.wait_for(self.wait_until(lambda: call_id in dict(self.tool_results)), 2)
        return dict(self.tool_results)[call_id]


class GatewayDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.credentials = LoopbackCredentials()
        self.alice = self.credentials.issue("alice", RunPermission)
        self.bob = self.credentials.issue("bob", RunPermission)
        self.backend = Backend()
        runs = DurableRunService(
            SQLiteRunStore(root / "runs.sqlite"), self.credentials, self.backend,
            admissions_open=True, backend_scope="scripted",
        )
        self.runtime = SimpleNamespace(
            runs=runs, credentials=self.credentials, journal=VoiceToolJournal(root / "voice.sqlite"),
            approve_query=lambda connection, query: (
                self.credentials.require(connection, RunPermission.SUBMIT) and query == "fixture"
            ),
        )
        self.enterContext(voice_env(GEMINI_API_KEY="fixture", GEMINI_LIVE_MODEL="fixture-model"))
        await self.enterAsyncContext(app.router.lifespan_context(app))
        app.state.durable_runtime = self.runtime
        self.addCleanup(delattr, app.state, "durable_runtime")

    @asynccontextmanager
    async def session(self, *, durable=True):
        provider = DelegatingProvider()
        with patch("avi.providers.gemini_live.GeminiLiveProvider", return_value=provider):
            async with connection() as (incoming, outgoing):
                self.assertEqual((await outgoing.get())["type"], "websocket.accept")
                start = _start()
                if durable:
                    start["capabilities"]["durable_runs"] = True
                await send(incoming, start)
                ready = await reply(outgoing)
                self.assertEqual(ready["type"], "session.ready")
                self.assertEqual(provider.delegation_enabled, durable)
                yield incoming, outgoing, provider

    async def authenticate(self, incoming, outgoing, token):
        await send(incoming, {"protocol": "avip/1", "type": "agent.run.authenticate", "token": token})
        self.assertEqual((await reply(outgoing))["type"], "agent.run.authenticated")

    async def test_media_only_and_unauthenticated_calls_cannot_acquire_privilege(self):
        args = {"query": "fixture"}
        async with self.session(durable=False) as (_, _, provider):
            self.assertEqual(await provider.invoke("one", DELEGATE_TOOL, args),
                             {"error": {"code": "access_denied"}})
        async with self.session() as (incoming, outgoing, provider):
            self.assertEqual(await provider.invoke("two", DELEGATE_TOOL, args),
                             {"error": {"code": "access_denied"}})
            await self.authenticate(incoming, outgoing, self.alice)
            await provider.emit(VoiceEvent("tool_call", {"id": "two", "name": DELEGATE_TOOL, "args": args}))
            self.assertEqual(await provider.invoke("three", DELEGATE_TOOL, {"query": "forbidden"}),
                             {"error": {"code": "approval_denied"}})
            self.assertEqual(await provider.invoke("four", "avi_ack", {}), {"output": {}})
            self.assertEqual(len(provider.tool_results), 3)
        self.assertEqual(self.backend.submissions, [])

    async def test_revoked_grant_at_reply_handoff_redacts_a_previously_fetched_result(self):
        async with self.session() as (incoming, outgoing, provider):
            await self.authenticate(incoming, outgoing, self.alice)
            receipt = await provider.invoke("submit", DELEGATE_TOOL, {"query": "fixture"})
            run_id = receipt["output"]["run_id"]
            transition = self.runtime.journal.transition

            def revoke_on_attempt(call, previous, target):
                if call.call_id == "read" and target == "attempted":
                    self.credentials.revoke(self.alice)
                return transition(call, previous, target)

            with patch.object(self.runtime.journal, "transition", side_effect=revoke_on_attempt):
                response = await provider.invoke("read", RESULT_TOOL, {"run_id": run_id})
            self.assertEqual(response, {"error": {"code": "access_denied"}})

    async def test_authenticated_result_on_new_socket_survives_interrupt_and_rejects_other_owner(self):
        async with self.session() as (incoming, outgoing, provider):
            await self.authenticate(incoming, outgoing, self.alice)
            receipt = await provider.invoke("submit", DELEGATE_TOOL, {"query": "fixture"})
            run_id = receipt["output"]["run_id"]
            await provider.emit(VoiceEvent("error", {"code": "tool_call_cancelled", "ids": ["submit"]}))
            await provider.emit(VoiceEvent("interrupted"))
            self.assertEqual((await reply(outgoing))["type"], "response.interrupted")
        for token, expected in ((self.bob, {"error": {"code": "access_denied"}}),
                                (self.alice, {"output": {"status": "completed", "run_id": run_id,
                                                         "result": "fixture result"}})):
            async with self.session() as (incoming, outgoing, provider):
                await self.authenticate(incoming, outgoing, token)
                self.assertEqual(await provider.invoke("read", RESULT_TOOL, {"run_id": run_id}), expected)
        self.assertEqual(self.backend.submissions, [("alice", run_id)])
