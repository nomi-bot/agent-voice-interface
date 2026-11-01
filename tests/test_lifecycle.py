import asyncio
import copy
import json
from pathlib import Path
import unittest

from avi.core.avip_session import AvipSession, _SessionEnd, _session_close_reason
from avi.core.lifecycle import (
    AGENT_RUN_TRANSITIONS,
    VOICE_RESPONSE_TRANSITIONS,
    VOICE_SESSION_TRANSITIONS,
    AgentRun,
    AgentRunState,
    InvalidTransition,
    SessionCloseReason,
    VoiceResponse,
    VoiceResponseState,
    VoiceSession,
    VoiceSessionState,
)
from avi.core.session import SessionLimits
from avi.protocols.avip import Capabilities
from avi.protocols.media import Direction, MediaFrame, active_mock_sinks, pack_frame
from avi.transports.base import TransportDisconnected


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "avip-v1-handshake.json").read_text()
)
START = FIXTURES["valid"][2]["message"]


def _snap_session(session):
    return (
        session.state, session.revision, session.history,
        session.session_id, session.request_id, session.device_id,
        session.close_reason,
    )


def _snap_response(response):
    return (
        response.state, response.revision, response.history,
        response.failure_code, response.playback_stops, response.cleaned,
        response.response_id, response.session_id, response.turn_id,
        response.request_id,
    )


def _snap_run(run):
    return (
        run.state, run.revision, run.history, run.result, run.failure_code,
        run.cleaned, run.run_id, run.session_id, run.request_id,
    )


class ScriptedTransport:
    """One scripted AVIP peer. An empty queue is a disconnect."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = None
        self.states = []
        self.runs = []
        self.session = None

    async def receive_message(self):
        if self.session is not None:
            voice = self.session.voice_session
            self.states.append(voice.state)
            if voice.session_id is not None and not self.runs:
                run = AgentRun("run-1", session_id=voice.session_id)
                run.start()
                self.runs.append(run)
        if not self.messages:
            raise TransportDisconnected
        return self.messages.pop(0)

    async def send(self, event):
        self.sent.append(event)

    async def send_bytes(self, payload):
        self.sent.append(payload)

    async def close(self, code, reason):
        self.closed = (code, reason)


class IdleTransport(ScriptedTransport):
    def __init__(self):
        super().__init__([])

    async def receive_message(self):
        if self.session is not None:
            self.states.append(self.session.voice_session.state)
        await asyncio.Event().wait()


class StallSend(ScriptedTransport):
    def __init__(self, message):
        super().__init__([("text", message)])

    async def send(self, event):
        self.sent.append(event)
        await asyncio.Event().wait()


class ContractTests(unittest.TestCase):
    def test_session_edges_match_the_lifecycle_contract(self):
        self.assertEqual(set(VOICE_SESSION_TRANSITIONS), set(VoiceSessionState))
        self.assertEqual(VOICE_SESSION_TRANSITIONS, {
            VoiceSessionState.CONNECTING: frozenset({
                VoiceSessionState.ACTIVE, VoiceSessionState.CLOSING,
            }),
            VoiceSessionState.ACTIVE: frozenset({
                VoiceSessionState.RECONNECTING, VoiceSessionState.CLOSING,
            }),
            VoiceSessionState.RECONNECTING: frozenset({
                VoiceSessionState.ACTIVE, VoiceSessionState.CLOSING,
            }),
            VoiceSessionState.CLOSING: frozenset({VoiceSessionState.CLOSED}),
            VoiceSessionState.CLOSED: frozenset(),
        })

    def test_response_edges_match_the_lifecycle_contract(self):
        self.assertEqual(set(VOICE_RESPONSE_TRANSITIONS), set(VoiceResponseState))
        self.assertEqual(VOICE_RESPONSE_TRANSITIONS, {
            VoiceResponseState.REQUESTED: frozenset({
                VoiceResponseState.STREAMING,
                VoiceResponseState.INTERRUPTED,
                VoiceResponseState.FAILED,
            }),
            VoiceResponseState.STREAMING: frozenset({
                VoiceResponseState.COMPLETED,
                VoiceResponseState.INTERRUPTED,
                VoiceResponseState.FAILED,
            }),
            VoiceResponseState.COMPLETED: frozenset(),
            VoiceResponseState.INTERRUPTED: frozenset(),
            VoiceResponseState.FAILED: frozenset(),
        })
        self.assertNotIn(
            VoiceResponseState.FAILED,
            VOICE_RESPONSE_TRANSITIONS[VoiceResponseState.INTERRUPTED],
        )
        self.assertNotIn(
            VoiceResponseState.INTERRUPTED,
            VOICE_RESPONSE_TRANSITIONS[VoiceResponseState.FAILED],
        )

    def test_run_edges_match_the_lifecycle_contract(self):
        self.assertEqual(set(AGENT_RUN_TRANSITIONS), set(AgentRunState))
        self.assertEqual(AGENT_RUN_TRANSITIONS, {
            AgentRunState.SUBMITTED: frozenset({
                AgentRunState.QUEUED, AgentRunState.RUNNING,
                AgentRunState.FAILED, AgentRunState.CANCELLED,
            }),
            AgentRunState.QUEUED: frozenset({
                AgentRunState.RUNNING, AgentRunState.FAILED, AgentRunState.CANCELLED,
            }),
            AgentRunState.RUNNING: frozenset({
                AgentRunState.COMPLETED, AgentRunState.FAILED, AgentRunState.CANCELLED,
            }),
            AgentRunState.COMPLETED: frozenset(),
            AgentRunState.FAILED: frozenset(),
            AgentRunState.CANCELLED: frozenset(),
        })
        self.assertNotIn(
            AgentRunState.CANCELLED, AGENT_RUN_TRANSITIONS[AgentRunState.FAILED],
        )
        self.assertNotIn(
            AgentRunState.FAILED, AGENT_RUN_TRANSITIONS[AgentRunState.CANCELLED],
        )
        self.assertNotIn(
            AgentRunState.COMPLETED, AGENT_RUN_TRANSITIONS[AgentRunState.QUEUED],
        )

    def test_unknown_local_close_is_a_failure(self):
        self.assertIs(
            _session_close_reason(_SessionEnd(1000, "Normal closure")),
            SessionCloseReason.FAILED,
        )
        self.assertIs(_session_close_reason(None), SessionCloseReason.DISCONNECTED)
        self.assertIs(
            _session_close_reason(_SessionEnd(1001, "Idle timeout")),
            SessionCloseReason.DISCONNECTED,
        )


class VoiceSessionTests(unittest.TestCase):
    def _active(self):
        session = VoiceSession()
        session.activate("sess-1", request_id="req-1", device_id="dev-1")
        return session

    def _reject(self, session, call):
        before = _snap_session(session)
        with self.assertRaises(InvalidTransition) as caught:
            call()
        self.assertEqual(_snap_session(session), before)
        self.assertEqual(caught.exception.kind, "VoiceSession")
        self.assertEqual(caught.exception.current, str(before[0]))

    def test_ready_reconnect_and_close_are_deterministic(self):
        def walk():
            session = self._active()
            session.lose_transport()
            session.reattach()
            session.cleanup(SessionCloseReason.ENDED)
            return session

        first, second = walk(), walk()
        self.assertEqual(first.history, second.history)
        self.assertEqual(
            [(step.source, step.target, step.operation) for step in first.history],
            [
                ("connecting", "active", "activate"),
                ("active", "reconnecting", "lose_transport"),
                ("reconnecting", "active", "reattach"),
                ("active", "closing", "begin_close"),
                ("closing", "closed", "finish_close"),
            ],
        )
        self.assertEqual(first.revision, len(first.history))
        self.assertEqual(first.session_id, "sess-1")
        self.assertIs(first.close_reason, SessionCloseReason.ENDED)
        self.assertIs(first.state, VoiceSessionState.CLOSED)

    def test_cleanup_from_connecting_never_becomes_active(self):
        session = VoiceSession()
        session.cleanup(SessionCloseReason.FAILED)
        self.assertIs(session.state, VoiceSessionState.CLOSED)
        self.assertIs(session.close_reason, SessionCloseReason.FAILED)
        self.assertIsNone(session.session_id)
        self.assertEqual([step.target for step in session.history], ["closing", "closed"])

    def test_same_reason_cleanup_is_idempotent_and_a_new_reason_is_rejected(self):
        session = self._active()
        session.begin_close(SessionCloseReason.FAILED)
        before = _snap_session(session)
        self._reject(session, lambda: session.cleanup(SessionCloseReason.DISCONNECTED))
        self.assertEqual(_snap_session(session), before)
        session.cleanup(SessionCloseReason.FAILED)
        self.assertIs(session.state, VoiceSessionState.CLOSED)
        closed = _snap_session(session)
        session.cleanup(SessionCloseReason.FAILED)
        self.assertEqual(_snap_session(session), closed)
        self._reject(session, lambda: session.cleanup(SessionCloseReason.ENDED))
        self.assertEqual(_snap_session(session), closed)

    def test_reconnect_keeps_the_session_id(self):
        session = self._active()
        session.lose_transport()
        self.assertIs(session.state, VoiceSessionState.RECONNECTING)
        self.assertEqual(session.session_id, "sess-1")
        session.reattach()
        self.assertEqual(session.session_id, "sess-1")
        session.cleanup(SessionCloseReason.ABANDONED)
        self.assertIs(session.close_reason, SessionCloseReason.ABANDONED)
        self.assertEqual(session.session_id, "sess-1")

    def test_illegal_edges_leave_the_session_unchanged(self):
        connecting = VoiceSession()
        active = self._active()
        reconnecting = self._active()
        reconnecting.lose_transport()
        closing = self._active()
        closing.begin_close(SessionCloseReason.ENDED)
        closed = self._active()
        closed.cleanup(SessionCloseReason.DISCONNECTED)
        cases = (
            (connecting, connecting.lose_transport),
            (connecting, connecting.reattach),
            (connecting, connecting.finish_close),
            (active, lambda: active.activate("sess-2")),
            (active, active.reattach),
            (active, active.finish_close),
            (reconnecting, lambda: reconnecting.activate("sess-1")),
            (reconnecting, reconnecting.lose_transport),
            (reconnecting, reconnecting.finish_close),
            (closing, lambda: closing.activate("sess-1")),
            (closing, closing.lose_transport),
            (closing, closing.reattach),
            (closing, lambda: closing.begin_close(SessionCloseReason.FAILED)),
            (closed, lambda: closed.activate("sess-1")),
            (closed, closed.lose_transport),
            (closed, closed.reattach),
            (closed, lambda: closed.begin_close(SessionCloseReason.ENDED)),
            (closed, closed.finish_close),
        )
        for session, call in cases:
            with self.subTest(state=session.state, operation=call):
                self._reject(session, call)

    def test_bad_identity_and_reason_do_not_move_the_session(self):
        session = VoiceSession()
        before = _snap_session(session)
        with self.assertRaises(ValueError) as caught:
            session.activate("bad id")
        self.assertNotIn("bad id", str(caught.exception))
        self.assertEqual(_snap_session(session), before)
        with self.assertRaises(ValueError):
            session.cleanup("failed")
        self.assertEqual(_snap_session(session), before)


class VoiceResponseTests(unittest.TestCase):
    def _response(self):
        return VoiceResponse(
            "resp-1", session_id="sess-1", turn_id="turn-1", request_id="req-1",
        )

    def _reject(self, response, call):
        before = _snap_response(response)
        with self.assertRaises(InvalidTransition) as caught:
            call()
        self.assertEqual(_snap_response(response), before)
        self.assertEqual(caught.exception.kind, "VoiceResponse")

    def test_stream_then_complete(self):
        response = self._response()
        response.start_streaming()
        response.complete()
        self.assertIs(response.state, VoiceResponseState.COMPLETED)
        self.assertIsNone(response.failure_code)
        self.assertEqual(
            [step.operation for step in response.history],
            ["start_streaming", "complete"],
        )
        response.cleanup()
        response.cleanup()
        self.assertTrue(response.cleaned)
        self.assertIs(response.state, VoiceResponseState.COMPLETED)

    def test_interrupt_is_not_a_failure(self):
        for starter in (lambda response: None, lambda response: response.start_streaming()):
            with self.subTest(starter=starter):
                response = self._response()
                starter(response)
                response.stop_playback()
                response.interrupt()
                self.assertIs(response.state, VoiceResponseState.INTERRUPTED)
                self.assertIsNone(response.failure_code)
                self.assertEqual(response.playback_stops, 1)
                self.assertNotIn("fail", [step.operation for step in response.history])
                response.cleanup()
                self.assertIs(response.state, VoiceResponseState.INTERRUPTED)

    def test_failure_is_not_an_interrupt(self):
        for starter in (lambda response: None, lambda response: response.start_streaming()):
            with self.subTest(starter=starter):
                response = self._response()
                starter(response)
                response.fail("provider_error")
                self.assertIs(response.state, VoiceResponseState.FAILED)
                self.assertEqual(response.failure_code, "provider_error")
                self.assertNotIn("interrupt", [step.operation for step in response.history])
                self._reject(response, response.interrupt)
                response.cleanup()
                self.assertEqual(response.failure_code, "provider_error")

    def test_stop_playback_never_changes_state(self):
        response = self._response()
        response.start_streaming()
        response.complete()
        before = _snap_response(response)
        response.stop_playback()
        self.assertEqual(response.playback_stops, 1)
        self.assertEqual(response.state, before[0])
        self.assertEqual(response.history, before[2])
        self.assertEqual(response.revision, before[1])

    def test_illegal_response_edges_and_bad_codes_do_not_apply(self):
        requested = self._response()
        streaming = self._response()
        streaming.start_streaming()
        completed = self._response()
        completed.start_streaming()
        completed.complete()
        interrupted = self._response()
        interrupted.interrupt()
        failed = self._response()
        failed.fail()
        cases = (
            (requested, requested.complete),
            (streaming, streaming.start_streaming),
            (completed, completed.complete),
            (completed, completed.interrupt),
            (completed, lambda: completed.fail("failed")),
            (interrupted, interrupted.interrupt),
            (interrupted, lambda: interrupted.fail("failed")),
            (interrupted, interrupted.complete),
            (failed, lambda: failed.fail("failed")),
            (failed, failed.interrupt),
            (failed, failed.complete),
            (requested, requested.cleanup),
            (streaming, streaming.cleanup),
        )
        for response, call in cases:
            with self.subTest(state=response.state, call=call):
                self._reject(response, call)
        before = _snap_response(streaming)
        with self.assertRaises(ValueError) as caught:
            streaming.fail("cancelled")
        self.assertNotIn("cancelled", str(caught.exception))
        self.assertEqual(_snap_response(streaming), before)


class AgentRunTests(unittest.TestCase):
    def _reject(self, run, call):
        before = _snap_run(run)
        with self.assertRaises(InvalidTransition) as caught:
            call()
        self.assertEqual(_snap_run(run), before)
        self.assertEqual(caught.exception.kind, "AgentRun")

    def test_queue_then_complete_is_deterministic(self):
        def walk():
            run = AgentRun("run-1", session_id="sess-1", request_id="req-1")
            run.enqueue()
            run.start()
            run.complete("done")
            return run

        first, second = walk(), walk()
        self.assertEqual(first.history, second.history)
        self.assertEqual(
            [step.operation for step in first.history],
            ["enqueue", "start", "complete"],
        )
        self.assertEqual(first.result, "done")
        self.assertIs(first.state, AgentRunState.COMPLETED)
        first.cleanup()
        first.cleanup()
        self.assertTrue(first.cleaned)
        self.assertIs(first.state, AgentRunState.COMPLETED)

    def test_direct_start_skips_the_queue(self):
        run = AgentRun("run-direct")
        run.start()
        run.complete()
        self.assertIsNone(run.result)
        self.assertEqual(run.history[0].source, "submitted")
        self.assertEqual(run.history[0].target, "running")
        self.assertIsNone(run.session_id)

    def test_cancel_is_not_failure_from_every_live_state(self):
        submitted = AgentRun("run-submitted")
        queued = AgentRun("run-queued")
        queued.enqueue()
        running = AgentRun("run-running")
        running.start()
        for run in (submitted, queued, running):
            with self.subTest(state=run.state):
                run.cancel()
                self.assertIs(run.state, AgentRunState.CANCELLED)
                self.assertIsNone(run.failure_code)
                self.assertNotIn("fail", [step.operation for step in run.history])
                self._reject(run, lambda: run.fail("failed"))
                self._reject(run, run.cancel)
                run.cleanup()
                self.assertIs(run.state, AgentRunState.CANCELLED)

    def test_failure_is_not_cancellation_from_every_live_state(self):
        submitted = AgentRun("run-submitted")
        queued = AgentRun("run-queued")
        queued.enqueue()
        running = AgentRun("run-running")
        running.start()
        for run in (submitted, queued, running):
            with self.subTest(state=run.state):
                run.fail("timeout")
                self.assertIs(run.state, AgentRunState.FAILED)
                self.assertEqual(run.failure_code, "timeout")
                self.assertNotIn("cancel", [step.operation for step in run.history])
                self._reject(run, run.cancel)
                run.cleanup()
                self.assertIs(run.state, AgentRunState.FAILED)
                self.assertEqual(run.failure_code, "timeout")

    def test_live_cleanup_and_illegal_edges_do_not_choose_an_outcome(self):
        submitted = AgentRun("run-submitted")
        self._reject(submitted, submitted.complete)
        queued = AgentRun("run-queued")
        queued.enqueue()
        self._reject(queued, queued.complete)
        self._reject(queued, queued.enqueue)
        self._reject(queued, queued.cleanup)
        self.assertFalse(queued.cleaned)
        running = AgentRun("run-running")
        running.start()
        self._reject(running, running.enqueue)
        self._reject(running, running.cleanup)
        before = _snap_run(running)
        with self.assertRaises(ValueError):
            running.complete("x" * 4097)
        with self.assertRaises(ValueError):
            running.fail("cancelled")
        self.assertEqual(_snap_run(running), before)
        running.complete("x" * 4096)
        self.assertEqual(len(running.result), 4096)
        self._reject(running, running.cancel)


class SeparationTests(unittest.TestCase):
    def test_playback_interrupt_and_session_close_do_not_cancel_a_run(self):
        session = VoiceSession()
        session.activate("sess-1", request_id="req-1", device_id="dev-1")
        response = VoiceResponse(
            "resp-1", session_id="sess-1", turn_id="turn-1", request_id="req-1",
        )
        run = AgentRun("run-1", session_id="sess-1", request_id="req-1")
        run.enqueue()
        response.start_streaming()
        response.stop_playback()
        self.assertIs(response.state, VoiceResponseState.STREAMING)
        self.assertIs(run.state, AgentRunState.QUEUED)
        response.interrupt()
        self.assertIsNone(response.failure_code)
        self.assertIs(run.state, AgentRunState.QUEUED)
        session.lose_transport()
        session.cleanup(SessionCloseReason.ABANDONED)
        self.assertIs(session.state, VoiceSessionState.CLOSED)
        self.assertIs(response.state, VoiceResponseState.INTERRUPTED)
        self.assertIs(run.state, AgentRunState.QUEUED)
        run.start()
        run.cancel()
        self.assertIs(run.state, AgentRunState.CANCELLED)
        self.assertIs(response.state, VoiceResponseState.INTERRUPTED)
        self.assertIs(session.close_reason, SessionCloseReason.ABANDONED)
        failed = AgentRun("run-2", session_id="sess-1")
        failed.start()
        failed.fail("provider_error")
        self.assertIs(failed.state, AgentRunState.FAILED)
        self.assertIsNot(failed.state, run.state)


class AvipLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _start(self):
        return copy.deepcopy(START)

    async def _run(self, transport, limits=None, available=None):
        session = AvipSession(
            transport,
            SessionLimits() if limits is None else limits,
            Capabilities() if available is None else available,
        )
        transport.session = session
        self.assertIs(session.voice_session.state, VoiceSessionState.CONNECTING)
        await session.run()
        return session

    async def test_ready_then_disconnect_closes_without_cancelling_a_correlated_run(self):
        transport = ScriptedTransport([("text", self._start())])
        session = await self._run(transport)
        voice = session.voice_session
        ready = transport.sent[0]
        self.assertEqual(ready["type"], "session.ready")
        self.assertEqual(voice.session_id, ready["session_id"])
        self.assertEqual(voice.request_id, "req-control-001")
        self.assertEqual(voice.device_id, "control-001")
        self.assertIsNone(session.session_id)
        self.assertIs(voice.state, VoiceSessionState.CLOSED)
        self.assertIs(voice.close_reason, SessionCloseReason.DISCONNECTED)
        self.assertEqual(
            [step.operation for step in voice.history],
            ["activate", "begin_close", "finish_close"],
        )
        self.assertEqual(transport.states, [
            VoiceSessionState.CONNECTING, VoiceSessionState.ACTIVE,
        ])
        self.assertIsNone(transport.closed)
        self.assertEqual(len(transport.runs), 1)
        self.assertIs(transport.runs[0].state, AgentRunState.RUNNING)
        self.assertEqual(transport.runs[0].session_id, voice.session_id)
        self.assertTrue(all(isinstance(item, dict) for item in transport.sent))
        self.assertNotIn("reconnecting", [step.target for step in voice.history])

    async def test_media_does_not_change_the_session_state(self):
        message = copy.deepcopy(FIXTURES["valid"][0]["message"])
        frame = pack_frame(MediaFrame(Direction.AUDIO_UPLINK, 0, 0, b"\x01\x00"))
        transport = ScriptedTransport([("text", message), ("binary", frame)])
        session = await self._run(transport, available=Capabilities(audio_input=True))
        self.assertIs(session.voice_session.close_reason, SessionCloseReason.DISCONNECTED)
        self.assertEqual(
            [step.operation for step in session.voice_session.history],
            ["activate", "begin_close", "finish_close"],
        )
        self.assertEqual(
            [item["type"] for item in transport.sent if isinstance(item, dict)],
            ["session.ready"],
        )
        self.assertIsNone(transport.closed)
        self.assertIsNone(session.sink)
        self.assertIsNone(session.delivery)
        self.assertEqual(active_mock_sinks(), frozenset())

    async def test_media_protocol_error_fails_and_stops_the_sink(self):
        message = copy.deepcopy(FIXTURES["valid"][0]["message"])
        transport = ScriptedTransport([("text", message), ("binary", b"NO")])
        session = await self._run(
            transport,
            available=Capabilities(audio_input=True, audio_output=True),
        )
        self.assertIs(session.voice_session.state, VoiceSessionState.CLOSED)
        self.assertIs(session.voice_session.close_reason, SessionCloseReason.FAILED)
        self.assertEqual(transport.closed, (1002, "Protocol error"))
        self.assertIsNone(session.sink)
        self.assertEqual(active_mock_sinks(), frozenset())
        self.assertFalse(any(
            task.get_name() == "avip-mock-media-sink" for task in asyncio.all_tasks()
        ))

    async def test_idle_after_ready_disconnects_and_stops_the_sink(self):
        class IdleAfterReady(ScriptedTransport):
            async def receive_message(self):
                if self.messages:
                    return await ScriptedTransport.receive_message(self)
                await asyncio.Event().wait()

        message = copy.deepcopy(FIXTURES["valid"][0]["message"])
        transport = IdleAfterReady([("text", message)])
        session = await self._run(
            transport,
            SessionLimits(idle_timeout=0.05),
            Capabilities(audio_input=True),
        )
        self.assertIs(session.voice_session.close_reason, SessionCloseReason.DISCONNECTED)
        self.assertTrue(session.voice_session.session_id)
        self.assertEqual(transport.closed, (1001, "Idle timeout"))
        self.assertIsNone(session.sink)
        self.assertEqual(active_mock_sinks(), frozenset())

    async def test_stalled_mirror_fails_and_stops_the_sink(self):
        class StallBytes(ScriptedTransport):
            async def send_bytes(self, payload):
                self.sent.append(payload)
                await asyncio.Event().wait()

        message = copy.deepcopy(FIXTURES["valid"][0]["message"])
        frame = pack_frame(MediaFrame(Direction.AUDIO_UPLINK, 0, 0, b"\x01\x00"))
        transport = StallBytes([("text", message), ("binary", frame)])
        session = await self._run(
            transport,
            SessionLimits(send_timeout=0.05),
            Capabilities(audio_input=True, audio_output=True),
        )
        self.assertIs(session.voice_session.close_reason, SessionCloseReason.FAILED)
        self.assertEqual(transport.closed, (1013, "Send timeout"))
        self.assertIsNone(session.sink)
        self.assertEqual(active_mock_sinks(), frozenset())

    async def test_protocol_failures_close_as_failed_and_never_activate(self):
        cases = (
            ("text", {"type": "nope"}, 1002, "Protocol error"),
            ("binary", b"early", 1003, "Unsupported data"),
        )
        for kind, payload, code, reason in cases:
            with self.subTest(kind=kind):
                transport = ScriptedTransport([(kind, payload)])
                session = await self._run(transport)
                voice = session.voice_session
                self.assertIs(voice.state, VoiceSessionState.CLOSED)
                self.assertIs(voice.close_reason, SessionCloseReason.FAILED)
                self.assertIsNone(voice.session_id)
                self.assertEqual(
                    [step.operation for step in voice.history],
                    ["begin_close", "finish_close"],
                )
                self.assertEqual(transport.closed, (code, reason))
                self.assertNotIn(VoiceSessionState.ACTIVE, transport.states)

    async def test_duplicate_start_fails_after_the_session_is_active(self):
        start = self._start()
        transport = ScriptedTransport([("text", start), ("text", start)])
        session = await self._run(transport)
        voice = session.voice_session
        self.assertIs(voice.close_reason, SessionCloseReason.FAILED)
        self.assertEqual(voice.history[0].operation, "activate")
        self.assertEqual(transport.closed, (1002, "Protocol error"))
        self.assertEqual(transport.sent[1]["code"], "session_already_started")
        self.assertTrue(voice.session_id)

    async def test_idle_timeout_is_a_disconnect_and_send_timeout_is_a_failure(self):
        idle = IdleTransport()
        idle_session = await self._run(idle, SessionLimits(idle_timeout=0.05))
        self.assertIs(idle_session.voice_session.close_reason, SessionCloseReason.DISCONNECTED)
        self.assertIsNone(idle_session.voice_session.session_id)
        self.assertEqual(idle.closed, (1001, "Idle timeout"))
        self.assertEqual(idle.states, [VoiceSessionState.CONNECTING])

        stalled = StallSend(self._start())
        stalled_session = await self._run(stalled, SessionLimits(send_timeout=0.05))
        self.assertIs(
            stalled_session.voice_session.close_reason, SessionCloseReason.FAILED,
        )
        self.assertIsNone(stalled_session.voice_session.session_id)
        self.assertEqual(stalled.closed, (1013, "Send timeout"))
        self.assertNotIn("activate", [
            step.operation for step in stalled_session.voice_session.history
        ])
