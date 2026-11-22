"""Exercise the real router with a scripted provider and a durable-run double."""

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from avi.core import voice_delegation
from avi.core.events import VoiceEvent
from avi.core.lifecycle import AgentRunState


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "jev29-voice-delegation-v1.json").read_text())


class RunConflict(ValueError):
    pass


class AdmissionsClosed(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    run_id: str
    state: AgentRunState = AgentRunState.SUBMITTED
    failure_code: str | None = None
    reconciliation_pending: bool = False


@dataclass(frozen=True)
class RetainedResult:
    status: Snapshot
    result: str | None
    retained: bool


class ScriptedProvider:
    def __init__(self):
        self.replies = []
        self.fail_reply = False

    async def tool_result(self, call_id, result):
        self.replies.append((call_id, result))
        if self.fail_reply:
            raise ConnectionError("private provider diagnostic")


class ScriptedRuns:
    def __init__(self):
        self.submissions = {}
        self.owners = {}
        self.results = {}
        self.dispatch_started = asyncio.Event()
        self.release_dispatch = asyncio.Event()
        self.release_dispatch.set()
        self.revoked = set()
        self.next_snapshot = None

    async def submit(self, connection, request_id, kind, arguments):
        if connection in self.revoked:
            raise PermissionError("private authorization diagnostic")
        key = (connection, request_id)
        if key in self.submissions:
            payload, run = self.submissions[key]
            if payload != (kind, arguments):
                raise RunConflict("private request diagnostic")
            return run
        run = self.next_snapshot or Snapshot(f"run-{len(self.submissions) + 1}")
        self.owners[run.run_id] = connection
        self.submissions[key] = ((kind, arguments), run)
        self.dispatch_started.set()
        await self.release_dispatch.wait()
        return run

    async def result(self, connection, run_id):
        if connection in self.revoked or self.owners.get(run_id) != connection:
            raise PermissionError("private owner diagnostic")
        return self.results[run_id]

    def complete(self, run_id, text):
        self.results[run_id] = RetainedResult(Snapshot(run_id, AgentRunState.COMPLETED), text, True)


def tool(call_id, name, **arguments):
    return VoiceEvent("tool_call", {"id": call_id, "name": name, "args": arguments})


def start(router, event, *, turn_id="turn-1", response_id="response-1"):
    return router.accept(event, turn_id=turn_id, response_id=response_id)


class DelegationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = voice_delegation
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "voice-calls.sqlite3"
        self.journal = self.api.VoiceToolJournal(self.path)
        self.provider = ScriptedProvider()
        self.runs = ScriptedRuns()
        self.approved = True
        self.router = self.session()

    def session(self, *, connection="alice", session_id="session-1", provider=None, **kwargs):
        return self.api.VoiceToolSession(
            session_id=session_id,
            connection=connection,
            provider=provider or self.provider,
            runs=self.runs,
            approve_query=lambda connection, query: self.approved,
            journal=self.journal,
            run_conflict=RunConflict,
            admissions_closed=AdmissionsClosed,
            **kwargs,
        )

    async def test_submit_uses_one_allowlisted_task_and_persists_delivery_correlation(self):
        event = tool("call-1", self.api.DELEGATE_TOOL, query="Summarize the public project overview")
        await start(self.router, event)

        record = self.api.VoiceToolJournal(self.path).read("session-1", "call-1")
        self.assertEqual((record.turn_id, record.response_id, record.run_id), ("turn-1", "response-1", "run-1"))
        self.assertEqual(record.delivery, "acknowledged")
        self.assertEqual(self.runs.submissions["alice", record.request_id][0],
                         ("read_only", {"query": "Summarize the public project overview"}))
        self.assertEqual(self.provider.replies, [("call-1", {"output": {"status": "submitted", "run_id": "run-1"}})])

    async def test_same_call_is_not_dispatched_or_replied_twice(self):
        event = tool("call-1", self.api.DELEGATE_TOOL, query="Read an overview")
        self.runs.release_dispatch.clear()
        pending = start(self.router, event)
        await self.runs.dispatch_started.wait()
        repeated = start(self.router, event, turn_id="turn-2", response_id="response-2")
        self.assertIs(repeated, pending)
        self.runs.release_dispatch.set()
        await pending
        await start(self.session(), event)
        self.assertEqual(len(self.runs.submissions), 1)
        self.assertEqual(len(self.provider.replies), 1)
        self.assertEqual(self.journal.read("session-1", "call-1").turn_id, "turn-1")

    async def test_conflicting_redelivery_has_no_second_dispatch_or_reply(self):
        await start(self.router, tool("call-1", self.api.DELEGATE_TOOL, query="Query A"))
        with self.assertRaises(self.api.VoiceCallConflict):
            await start(self.session(), tool("call-1", self.api.DELEGATE_TOOL, query="Query B"))
        self.assertEqual(len(self.runs.submissions), 1)
        self.assertEqual(len(self.provider.replies), 1)

    async def test_unknown_and_malformed_tools_never_dispatch(self):
        events = (
            (tool("unknown", "other_tool", query="Read an overview"), "unknown_tool"),
            (tool("owner", self.api.DELEGATE_TOOL, query="A", owner="bob"), "invalid_arguments"),
            (tool("handle", self.api.RESULT_TOOL, handle="upstream-opaque"), "invalid_arguments"),
            (tool("blank", self.api.DELEGATE_TOOL, query="  \t"), "invalid_arguments"),
            (tool("long", self.api.DELEGATE_TOOL, query="📝" * 1024), "invalid_arguments"),
        )
        for event, expected in events:
            with self.subTest(call=event.payload["id"]):
                await start(self.router, event)
                self.assertEqual(self.provider.replies[-1][1], {"error": {"code": expected}})
        self.assertEqual(self.runs.submissions, {})

    async def test_approval_denial_does_not_dispatch(self):
        self.approved = False
        await start(self.router, tool("call-1", self.api.DELEGATE_TOOL, query="Read a summary"))
        self.assertEqual(self.runs.submissions, {})
        self.assertEqual(self.provider.replies, [("call-1", {"error": {"code": "approval_denied"}})])

    async def test_missing_or_revoked_authentication_does_not_dispatch(self):
        self.runs.revoked.add("alice")
        event = tool("call-1", self.api.DELEGATE_TOOL, query="Read a summary")
        await start(self.router, event)
        await start(self.session(connection=None, session_id="anonymous"), event)
        self.assertEqual(self.runs.submissions, {})
        self.assertEqual(self.provider.replies, [("call-1", {"error": {"code": "access_denied"}})] * 2)

    async def test_uncertain_admission_preserves_run_for_reconciliation(self):
        self.runs.next_snapshot = Snapshot("run-1", reconciliation_pending=True)
        scenario = FIXTURE["uncertain_submission"]
        await start(self.router, VoiceEvent(**scenario["event"]))
        self.assertEqual(self.journal.read("session-1", "call-1").run_id, "run-1")
        self.assertEqual(self.provider.replies, [("call-1", scenario["reply"])])

    async def test_submit_timeout_never_creates_a_new_request(self):
        self.runs.release_dispatch.clear()
        event = tool("call-1", self.api.DELEGATE_TOOL, query="Read slowly")
        router = self.session(operation_timeout=0.01)
        await start(router, event)
        await start(router, event)
        self.assertEqual(len(self.runs.submissions), 1)
        self.assertEqual(self.provider.replies, [("call-1", {"output": {"status": "pending_reconciliation"}})])

    async def test_playback_interruption_alone_keeps_the_original_call_replyable(self):
        self.runs.release_dispatch.clear()
        scenario = FIXTURE["submission"]
        pending = start(self.router, VoiceEvent(**scenario["event"]))
        await self.runs.dispatch_started.wait()
        interrupted = FIXTURE["interruption"]["playback_event"]
        self.assertIsNone(start(self.router, VoiceEvent(**interrupted)))
        self.assertIsNone(start(self.router, VoiceEvent("audio", b"\x00\x00")))
        self.assertFalse(pending.done())
        self.runs.release_dispatch.set()
        await pending
        self.assertEqual(self.provider.replies, [("call-1", scenario["reply"])])

    async def test_reported_upstream_failure_exposes_only_a_bounded_code(self):
        self.runs.next_snapshot = Snapshot("run-1", AgentRunState.FAILED, "private failure detail")
        await start(self.router, VoiceEvent(**FIXTURE["submission"]["event"]))
        self.assertEqual(self.provider.replies, [("call-1", {"error": {"code": "run_failed"}})])
        self.assertEqual(self.journal.read("session-1", "call-1").run_id, "run-1")

    async def test_interruption_and_disconnect_leave_task_and_result_available(self):
        self.runs.release_dispatch.clear()
        submitted = FIXTURE["submission"]
        interrupted = FIXTURE["interruption"]
        retained = FIXTURE["retained_result"]
        pending = start(self.router, VoiceEvent(**submitted["event"]))
        await self.runs.dispatch_started.wait()
        self.assertIsNone(start(self.router, VoiceEvent(**interrupted["playback_event"])))
        self.assertFalse(pending.done())
        self.assertIsNone(start(self.router, VoiceEvent(**interrupted["call_expiry_event"])))
        self.router.close()
        self.runs.release_dispatch.set()
        await pending
        record = self.journal.read("session-1", "call-1")
        self.assertIsNone(interrupted["old_call_reply"])
        self.assertEqual(self.provider.replies, [])
        self.assertEqual((record.delivery, record.run_id), ("expired", "run-1"))
        self.runs.complete(record.run_id, retained["reply"]["output"]["result"])
        next_provider = ScriptedProvider()
        next_session = self.session(session_id="session-2", provider=next_provider)
        await start(next_session, VoiceEvent(**retained["event"]))
        self.assertEqual(next_provider.replies, [("call-1", retained["reply"])])
        self.assertEqual(len(self.runs.submissions), 1)

    async def test_two_users_with_matching_provider_call_ids_have_distinct_runs(self):
        bob_provider = ScriptedProvider()
        bob = self.session(connection="bob", session_id="session-2", provider=bob_provider)
        event = tool("shared-call", self.api.DELEGATE_TOOL, query="Read a summary")
        await asyncio.gather(start(self.router, event), start(bob, event))
        self.assertEqual(len(self.runs.submissions), 2)
        runs = {owner: run.run_id for (owner, _), (_, run) in self.runs.submissions.items()}
        self.assertNotEqual(runs["alice"], runs["bob"])
        self.runs.complete(runs["alice"], "Alice result")
        await start(bob, tool("guess", self.api.RESULT_TOOL, run_id=runs["alice"]))
        await start(bob, tool("missing", self.api.RESULT_TOOL, run_id="no-such-run"))
        self.assertEqual([reply for _, reply in bob_provider.replies[-2:]], [FIXTURE["access_denied"]] * 2)

    async def test_uncertain_delivery_is_recorded_and_not_resent(self):
        self.provider.fail_reply = True
        event = tool("call-1", self.api.DELEGATE_TOOL, query="Read a summary")
        await start(self.router, event)
        self.assertEqual(self.journal.read("session-1", "call-1").delivery, "attempted")
        self.provider.fail_reply = False
        await start(self.session(), event)
        self.assertEqual(len(self.provider.replies), 1)

    async def test_result_reply_is_bounded_and_does_not_contain_the_private_payload(self):
        await start(self.router, tool("submit", self.api.DELEGATE_TOOL, query="PRIVATE_TASK_INPUT"))
        self.runs.complete("run-1", "📝" * 4096)
        await start(self.router, tool("result", self.api.RESULT_TOOL, run_id="run-1"))
        reply = self.provider.replies[-1][1]
        self.assertLessEqual(len(json.dumps(reply).encode("utf-8")), 16_384)
        self.assertLess(len(reply["output"]["result"]), 4096)
        self.assertNotIn(b"PRIVATE_TASK_INPUT", self.path.read_bytes())

    async def test_adapter_ack_does_not_submit_a_run(self):
        await start(self.router, tool("ack", "avi_ack"))
        self.assertEqual(self.provider.replies, [("ack", {"output": {}})])
        self.assertEqual(self.runs.submissions, {})

    async def test_full_router_refuses_additional_work_while_existing_call_runs(self):
        router = self.session(max_pending_calls=1)
        self.runs.release_dispatch.clear()
        pending = start(router, tool("first", self.api.DELEGATE_TOOL, query="Read slowly"))
        try:
            await self.runs.dispatch_started.wait()
            with self.assertRaises(self.api.VoiceToolOverload):
                start(router, tool("next", self.api.DELEGATE_TOOL, query="Read another"))
        finally:
            self.runs.release_dispatch.set()
            await pending
        self.assertEqual(len(self.runs.submissions), 1)


class GeminiDelegationToolTests(unittest.TestCase):
    def test_tool_declarations_are_opt_in_and_keep_the_ack_tool(self):
        from avi.providers.gemini_live import live_connect_config

        default = live_connect_config("NON_BLOCKING").tools[0].function_declarations
        enabled = live_connect_config("NON_BLOCKING", enable_delegation_tools=True).tools[0].function_declarations
        self.assertEqual([tool.name for tool in default], ["avi_ack"])
        self.assertEqual(enabled[0], default[0])
        self.assertEqual([tool.name for tool in enabled[1:]], [
            voice_delegation.DELEGATE_TOOL, voice_delegation.RESULT_TOOL,
        ])
        for declaration, field in zip(enabled[1:], ("query", "run_id"), strict=True):
            self.assertEqual(declaration.parameters.required, [field])
            self.assertIsNone(declaration.parameters.additional_properties)
            self.assertEqual(set(declaration.parameters.properties), {field})
