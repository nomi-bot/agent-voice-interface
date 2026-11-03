"""Mount decision and media bridge for Gemini Live on /ws/avip.

Credentialed coverage skips unless ``GEMINI_API_KEY`` is set. A key without
``GEMINI_LIVE_MODEL`` fails closed. Reports and assertion text do not include
the key, PCM, or a resumption handle.
"""
import asyncio
import copy
import json
import os
from contextlib import contextmanager
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from avi.core.avip_session import AvipSession
from avi.core.events import VoiceEvent
from avi.core.session import SessionLimits
from avi.main import app
from avi.protocols.avip import Capabilities
from avi.protocols.media import Direction, MediaFrame, active_mock_sinks, pack_frame, unpack_frame
from avi.providers.mount import (
    MODE_LIVE,
    MODE_MOCK,
    MODE_NOT_CONFIGURED,
    REASON_CONFIGURED,
    REASON_CREDENTIALS_ABSENT,
    REASON_MOCK_MEDIA,
    REASON_MODEL_UNCONFIGURED,
    resolve_voice_mount,
)
from avi.transports.base import TransportDisconnected
from test_avip_websocket import connection, send, send_frame


ROOT = Path(__file__).parents[1]
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "avip-v1-handshake.json").read_text())
SECRET = "AIzaSyMOUNTTESTSECRET"
HANDLE = "projects/p/sessions/s/handles/secret-handle"
MODEL = "operator-selected-model"
AUDIO = Capabilities(audio_input=True, audio_output=True)
_NAMES = ("GEMINI_API_KEY", "GEMINI_LIVE_MODEL", "GOOGLE_API_KEY", "AVIP_MOCK_MEDIA")


@contextmanager
def voice_env(**values):
    saved = {name: os.environ.get(name) for name in _NAMES}
    try:
        for name in _NAMES:
            os.environ.pop(name, None)
        for name, value in values.items():
            os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class FakeProvider:
    """In-process VoiceProvider. It does not open a socket."""

    def __init__(self, *, start_error=None, hang_start=False, tool_error=False):
        self.start_error = start_error
        self.hang_start = hang_start
        self.tool_error = tool_error
        self.started = False
        self.closed = False
        self.audio = []
        self.tool_results = []
        self.reply = None
        self._events = asyncio.Queue()
        self._progress = asyncio.Event()
        self._subscribed = False

    async def start(self):
        if self.hang_start:
            await asyncio.Event().wait()
        if self.start_error is not None:
            raise self.start_error
        self.started = True

    async def send_audio(self, pcm: bytes):
        if not self.started:
            raise RuntimeError("provider is not open")
        self.audio.append(bytes(pcm))
        reply = self.reply
        self.reply = None
        if reply is not None:
            await self._events.put(VoiceEvent(kind="audio", payload=reply))
        self._progress.set()

    def events(self):
        if self._subscribed:
            raise RuntimeError("events already subscribed")
        self._subscribed = True
        return self._iterate()

    async def _iterate(self):
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def emit(self, event):
        await self._events.put(event)
        self._progress.set()

    async def tool_result(self, call_id, result):
        if self.tool_error:
            raise RuntimeError("tool failed")
        self.tool_results.append((call_id, result))
        self._progress.set()

    async def cancel_response(self):
        raise AssertionError("the gateway must not cancel from the media bridge")

    async def close(self):
        self.closed = True
        await self._events.put(None)
        self._progress.set()

    async def wait_until(self, predicate):
        while not predicate():
            self._progress.clear()
            if predicate():
                return
            await self._progress.wait()


class VirtualClock:
    """Pacer clock. sleep() advances time and does not wait on the wall clock."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


class Gate:
    def __init__(self):
        self.inbox = asyncio.Queue()
        self.sent = []
        self.closed = None
        self._changed = asyncio.Event()
        self._seen = 0

    async def receive_message(self):
        item = await self.inbox.get()
        if item is None:
            raise TransportDisconnected
        return item

    async def send(self, event):
        self._note(event)

    async def send_bytes(self, payload):
        self._note(payload)

    async def close(self, code, reason):
        self.closed = (code, reason)
        self._changed.set()

    def _note(self, item):
        self.sent.append(item)
        self._changed.set()

    async def next_item(self):
        while self._seen >= len(self.sent):
            self._changed.clear()
            if self._seen < len(self.sent):
                break
            await self._changed.wait()
        item = self.sent[self._seen]
        self._seen += 1
        return item


def _start(index=0):
    return copy.deepcopy(FIXTURES["valid"][index]["message"])


def _uplink(payload, sequence=0, timestamp=0):
    return pack_frame(MediaFrame(Direction.AUDIO_UPLINK, sequence, timestamp, payload))


def _text_types(sent):
    return [item.get("type") for item in sent if isinstance(item, dict)]


def _provider_tasks():
    names = {"avip-voice-provider", "avip-receive"}
    return [task for task in asyncio.all_tasks() if task.get_name() in names]


class VoiceMountTests(unittest.TestCase):
    def test_matrix_keeps_the_key_and_model_out_of_the_decision(self):
        secret_model = "hidden/model"
        cases = (
            ({}, MODE_NOT_CONFIGURED, REASON_CREDENTIALS_ABSENT, False),
            ({"GOOGLE_API_KEY": SECRET, "GEMINI_LIVE_MODEL": MODEL}, MODE_NOT_CONFIGURED, REASON_CREDENTIALS_ABSENT, False),
            ({"GEMINI_API_KEY": "  ", "GEMINI_LIVE_MODEL": MODEL}, MODE_NOT_CONFIGURED, REASON_CREDENTIALS_ABSENT, False),
            ({"GEMINI_API_KEY": SECRET}, MODE_NOT_CONFIGURED, REASON_MODEL_UNCONFIGURED, False),
            ({"GEMINI_API_KEY": SECRET, "GEMINI_LIVE_MODEL": "  "}, MODE_NOT_CONFIGURED, REASON_MODEL_UNCONFIGURED, False),
            ({"GEMINI_API_KEY": SECRET, "GEMINI_LIVE_MODEL": secret_model}, MODE_NOT_CONFIGURED, REASON_MODEL_UNCONFIGURED, False),
            ({"AVIP_MOCK_MEDIA": "1"}, MODE_MOCK, REASON_MOCK_MEDIA, True),
            ({"AVIP_MOCK_MEDIA": "1", "GEMINI_API_KEY": SECRET}, MODE_MOCK, REASON_MODEL_UNCONFIGURED, True),
            ({"AVIP_MOCK_MEDIA": "true"}, MODE_NOT_CONFIGURED, REASON_CREDENTIALS_ABSENT, False),
            ({"GEMINI_API_KEY": SECRET, "GEMINI_LIVE_MODEL": MODEL}, MODE_LIVE, REASON_CONFIGURED, True),
            (
                {"GEMINI_API_KEY": SECRET, "GEMINI_LIVE_MODEL": MODEL, "AVIP_MOCK_MEDIA": "1"},
                MODE_LIVE, REASON_CONFIGURED, True,
            ),
        )
        for env, mode, reason, audio in cases:
            with self.subTest(env=sorted(env)):
                mount = resolve_voice_mount(env)
                self.assertEqual(mount.mode, mode)
                self.assertEqual(mount.reason, reason)
                self.assertEqual(mount.capabilities.audio_input, audio)
                self.assertEqual(mount.capabilities.audio_output, audio)
                self.assertFalse(mount.capabilities.camera)
                self.assertFalse(mount.capabilities.robot_actions)
                rendered = repr(mount)
                self.assertNotIn(SECRET, rendered)
                self.assertNotIn(secret_model, rendered)
                self.assertNotIn(MODEL, rendered)
                self.assertNotIn("AIza", rendered)

    def test_health_names_mock_live_and_not_configured(self):
        async def read():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                return (await client.get("/health")).json()

        expectations = (
            ({}, MODE_NOT_CONFIGURED, REASON_CREDENTIALS_ABSENT),
            ({"GEMINI_API_KEY": SECRET}, MODE_NOT_CONFIGURED, REASON_MODEL_UNCONFIGURED),
            ({"AVIP_MOCK_MEDIA": "1"}, MODE_MOCK, REASON_MOCK_MEDIA),
            ({"GEMINI_API_KEY": SECRET, "GEMINI_LIVE_MODEL": MODEL}, MODE_LIVE, REASON_CONFIGURED),
        )
        for env, mode, reason in expectations:
            with self.subTest(mode=mode, reason=reason):
                with voice_env(**env):
                    body = asyncio.run(read())
                self.assertEqual(body, {
                    "status": "ok",
                    "voice": mode,
                    "voice_reason": reason,
                    "agent": "mock",
                })
                self.assertNotIn(SECRET, json.dumps(body))
                self.assertNotIn(MODEL, json.dumps(body))


class ProviderBridgeTests(unittest.IsolatedAsyncioTestCase):
    def _session(self, transport, provider, available=AUDIO, limits=None, clock=None):
        kwargs = {}
        if clock is not None:
            kwargs["monotonic"] = clock.monotonic
            kwargs["sleeper"] = clock.sleep
        return AvipSession(
            transport,
            SessionLimits() if limits is None else limits,
            available,
            provider=provider,
            telemetry_path="provider" if provider is not None else "mock-mirror",
            telemetry_scenario="gemini-live" if provider is not None else "gateway",
            **kwargs,
        )

    async def _ready(self, transport, message=None):
        await transport.inbox.put(("text", _start() if message is None else message))
        ready = await asyncio.wait_for(transport.next_item(), 1)
        self.assertEqual(ready["type"], "session.ready")
        return ready

    async def test_mock_mirror_still_echoes_when_no_provider_is_attached(self):
        payload = b"\x11\x22"
        transport = Gate()
        session = self._session(transport, None)
        task = asyncio.create_task(session.run())
        try:
            await self._ready(transport)
            self.assertEqual(len(active_mock_sinks()), 1)
            await transport.inbox.put(("binary", _uplink(payload)))
            echo = await asyncio.wait_for(transport.next_item(), 1)
            self.assertEqual(unpack_frame(echo).payload, payload)
            self.assertEqual(_text_types(transport.sent), ["session.ready"])
        finally:
            await transport.inbox.put(None)
            await task
        self.assertEqual(active_mock_sinks(), frozenset())
        self.assertEqual(session.telemetry_report["path"], "mock-mirror")

    async def test_uplink_is_forwarded_and_downlink_is_provider_audio(self):
        uplink = b"\x11\x22\x33\x44"
        downlink = b"\x55\x66"
        fake = FakeProvider()
        fake.reply = downlink
        transport = Gate()
        session = self._session(transport, fake)
        task = asyncio.create_task(session.run())
        try:
            ready = await self._ready(transport)
            self.assertTrue(ready["capabilities"]["audio_input"])
            self.assertTrue(ready["capabilities"]["audio_output"])
            self.assertTrue(fake.started)
            self.assertEqual(active_mock_sinks(), frozenset())
            await transport.inbox.put(("binary", _uplink(uplink)))
            frame = unpack_frame(await asyncio.wait_for(transport.next_item(), 1))
            self.assertEqual(frame.direction, Direction.AUDIO_DOWNLINK)
            self.assertEqual(frame.sequence, 0)
            self.assertEqual(frame.timestamp_us, 0)
            self.assertEqual(frame.payload, downlink)
            self.assertNotEqual(frame.payload, uplink)
            self.assertEqual(fake.audio, [uplink])
            self.assertEqual(_text_types(transport.sent), ["session.ready"])
        finally:
            await transport.inbox.put(None)
            await task
        self.assertTrue(fake.closed)
        self.assertEqual(_provider_tasks(), [])
        report = json.dumps(session.telemetry_report)
        self.assertEqual(session.telemetry_report["path"], "provider")
        self.assertEqual(session.telemetry_report["scenario"], "gemini-live")
        self.assertEqual(session.telemetry_report["e2e"]["scope"], "provider")
        self.assertEqual(session.telemetry_report["counters"]["uplinkFrames"], 1)
        self.assertEqual(session.telemetry_report["counters"]["downlinkFrames"], 1)
        self.assertNotIn(uplink.hex(), report)
        self.assertNotIn(downlink.hex(), report)

    async def test_interrupt_flushes_queued_audio_and_the_next_response_plays(self):
        from avip_client.playback import PlaybackBuffer

        fake = FakeProvider()
        transport = Gate()
        task = asyncio.create_task(self._session(transport, fake).run())
        try:
            await self._ready(transport)
            first = b"\x11\x00" * 320
            second = b"\x22\x00" * 320
            await fake.emit(VoiceEvent(kind="audio", payload=first))
            queued = unpack_frame(await asyncio.wait_for(transport.next_item(), 1))
            self.assertEqual(queued.payload, first)
            await fake.emit(VoiceEvent(
                kind="interrupted",
                payload={"source": "server", "detail": SECRET, "handle": HANDLE},
            ))
            event = await asyncio.wait_for(transport.next_item(), 1)
            self.assertEqual(event, {
                "protocol": "avip/1",
                "type": "response.interrupted",
                "generation": 0,
            })
            speaker = PlaybackBuffer()
            self.assertTrue(speaker.enqueue(queued.payload))
            self.assertTrue(speaker.apply(event))
            self.assertEqual(speaker.queued, [])
            self.assertEqual(speaker.generation, 1)
            await fake.emit(VoiceEvent(kind="audio", payload=second))
            nxt = unpack_frame(await asyncio.wait_for(transport.next_item(), 1))
            self.assertEqual(nxt.payload, second)
            self.assertTrue(speaker.enqueue(nxt.payload))
            self.assertEqual(speaker.generation, 1)
            self.assertFalse(speaker.apply({
                "protocol": "avip/1",
                "type": "response.interrupted",
                "generation": 0,
            }))
            self.assertEqual(speaker.queued, [second])
            rendered = json.dumps(transport.sent, default=lambda item: item.hex())
            self.assertNotIn(SECRET, rendered)
            self.assertNotIn(HANDLE, rendered)
            self.assertNotIn("tool_call", rendered)
            self.assertNotIn("\"source\"", rendered)
        finally:
            await transport.inbox.put(None)
            await task

    async def test_provider_error_closes_without_the_detail(self):
        fake = FakeProvider()
        transport = Gate()
        task = asyncio.create_task(self._session(transport, fake).run())
        with self.assertLogs("avi.core.avip_session", level="WARNING") as captured:
            await self._ready(transport)
            await fake.emit(VoiceEvent(
                kind="error",
                payload={
                    "code": "go_away",
                    "detail": SECRET,
                    "handle": HANDLE,
                    "pcm": b"\x11\x22".hex(),
                },
            ))
            await task
        self.assertEqual(transport.closed, (1011, "Voice provider unavailable"))
        self.assertEqual(_text_types(transport.sent), ["session.ready"])
        logs = "\n".join(captured.output)
        self.assertIn("go_away", logs)
        self.assertNotIn(SECRET, logs)
        self.assertNotIn(HANDLE, logs)
        self.assertNotIn("1122", logs)
        self.assertTrue(fake.closed)

    async def test_odd_provider_audio_is_not_framed(self):
        fake = FakeProvider()
        transport = Gate()
        task = asyncio.create_task(self._session(transport, fake).run())
        await self._ready(transport)
        await fake.emit(VoiceEvent(kind="audio", payload=b"\x01\x02\x03"))
        await task
        self.assertEqual(transport.closed, (1011, "Voice provider unavailable"))
        self.assertFalse(any(isinstance(item, bytes) for item in transport.sent))

    async def test_multisecond_burst_is_intact_and_stays_inside_160ms(self):
        fake = FakeProvider()
        clock = VirtualClock()
        transport = Gate()
        session = self._session(transport, fake, clock=clock)
        task = asyncio.create_task(session.run())
        payload = bytes((index // 640) % 251 for index in range(96_000))
        try:
            await self._ready(transport)
            await fake.emit(VoiceEvent(kind="audio", payload=payload))
            got = bytearray()
            while len(got) < len(payload):
                frame = unpack_frame(await asyncio.wait_for(transport.next_item(), 1))
                self.assertLessEqual(len(frame.payload), 640)
                self.assertGreater(len(frame.payload), 0)
                self.assertEqual(len(frame.payload) % 2, 0)
                got.extend(frame.payload)
            self.assertEqual(bytes(got), payload)
            pacer = session._pacer
            self.assertIsNotNone(pacer)
            self.assertLessEqual(pacer.max_ahead_us, 160_000)
            self.assertLessEqual(pacer.high_water, 8)
            self.assertLessEqual(pacer.max_backlog_bytes, pacer_backlog_limit())
            self.assertGreater(clock.t, 2.0)
            self.assertLess(clock.t, 4.0)
        finally:
            await transport.inbox.put(None)
            await task

    async def test_silent_gap_does_not_dump_the_next_answer(self):
        fake = FakeProvider()
        clock = VirtualClock()
        transport = Gate()
        session = self._session(transport, fake, clock=clock)
        task = asyncio.create_task(session.run())
        first = b"\x11\x00" * 320
        second = bytes((index // 640) % 251 for index in range(96_000))
        trace: list[tuple[float, bytes]] = []
        try:
            await self._ready(transport)
            await fake.emit(VoiceEvent(kind="audio", payload=first))
            frame = unpack_frame(await asyncio.wait_for(transport.next_item(), 1))
            trace.append((clock.t, frame.payload))
            clock.t += 10
            await fake.emit(VoiceEvent(kind="audio", payload=second))
            got = bytearray()
            turn_times: list[float] = []
            while len(got) < len(second):
                nxt = unpack_frame(await asyncio.wait_for(transport.next_item(), 1))
                got.extend(nxt.payload)
                turn_times.append(clock.t)
                trace.append((clock.t, nxt.payload))
            self.assertEqual(bytes(got), second)
            self.assertGreater(turn_times[-1] - turn_times[0], 2.0)
            self.assertLess(turn_times[-1] - turn_times[0], 4.0)
            pacer = session._pacer
            self.assertIsNotNone(pacer)
            self.assertLessEqual(pacer.max_ahead_us, 160_000)
            self.assertLessEqual(pacer.max_backlog_bytes, pacer_backlog_limit())
            _assert_speaker_plays(trace)
        finally:
            await transport.inbox.put(None)
            await task

    async def test_stalled_sender_bounds_backlog_and_reads_interrupt(self):
        from avi.core.downlink import DownlinkPacer

        blocked = asyncio.Event()
        seen_control: list[str] = []

        async def send_frame(chunk: bytes) -> None:
            await blocked.wait()

        async def send_json(message: dict) -> None:
            seen_control.append(message["type"])

        async def on_control(event: VoiceEvent) -> None:
            seen_control.append(event.kind)

        async def events():
            for _ in range(100):
                yield VoiceEvent(kind="audio", payload=bytes(8192))
            yield VoiceEvent(kind="turn_complete", payload={})
            yield VoiceEvent(kind="interrupted", payload={"source": "server"})

        pacer = DownlinkPacer(send_frame, send_json)
        task = asyncio.create_task(pacer.run(events(), on_control))
        try:
            for _ in range(1000):
                if "response.interrupted" in seen_control and "turn_complete" in seen_control:
                    break
                await asyncio.sleep(0)
            self.assertIn("turn_complete", seen_control)
            self.assertIn("response.interrupted", seen_control)
            self.assertLessEqual(pacer.max_backlog_bytes, pacer_backlog_limit())
            self.assertLessEqual(pacer.backlog_bytes(), pacer_backlog_limit())
            self.assertGreater(pacer.overflow_bytes, 8 * 8192)
            self.assertLessEqual(pacer.high_water, 8)
        finally:
            blocked.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_input_only_negotiation_drops_downlink_frames(self):
        fake = FakeProvider()
        transport = Gate()
        task = asyncio.create_task(self._session(
            transport, fake, Capabilities(audio_input=True, audio_output=False),
        ).run())
        try:
            ready = await self._ready(transport)
            self.assertTrue(ready["capabilities"]["audio_input"])
            self.assertFalse(ready["capabilities"]["audio_output"])
            await transport.inbox.put(("binary", _uplink(b"\x10\x20")))
            await fake.wait_until(lambda: fake.audio == [b"\x10\x20"])
            await fake.emit(VoiceEvent(kind="audio", payload=b"\x30\x40"))
            await asyncio.sleep(0.05)
            self.assertFalse(any(isinstance(item, bytes) for item in transport.sent))
        finally:
            await transport.inbox.put(None)
            await task

    async def test_a_willing_bridge_accepts_more_than_eight_uplink_frames(self):
        fake = FakeProvider()
        transport = Gate()
        task = asyncio.create_task(self._session(transport, fake).run())
        try:
            await self._ready(transport)
            for sequence in range(9):
                await transport.inbox.put(("binary", _uplink(b"\x01\x00", sequence, sequence)))
            await fake.wait_until(lambda: len(fake.audio) == 9)
            self.assertEqual(_text_types(transport.sent), ["session.ready"])
        finally:
            await transport.inbox.put(None)
            await task

    async def test_start_failure_does_not_send_session_ready(self):
        fake = FakeProvider(start_error=RuntimeError("connect failed " + SECRET))
        transport = Gate()
        task = asyncio.create_task(self._session(transport, fake).run())
        with self.assertLogs("avi.core.avip_session", level="WARNING") as captured:
            await transport.inbox.put(("text", _start()))
            await task
        self.assertFalse(fake.started)
        self.assertTrue(fake.closed)
        self.assertEqual(transport.closed, (1011, "Voice provider unavailable"))
        self.assertEqual(transport.sent, [])
        self.assertNotIn(SECRET, "\n".join(captured.output))

    async def test_start_timeout_closes_before_ready(self):
        fake = FakeProvider(hang_start=True)
        transport = Gate()
        session = self._session(
            transport, fake, limits=SessionLimits(operation_timeout=0.05),
        )
        task = asyncio.create_task(session.run())
        await transport.inbox.put(("text", _start()))
        await task
        self.assertEqual(transport.closed, (1011, "Voice provider unavailable"))
        self.assertEqual(transport.sent, [])
        self.assertTrue(fake.closed)

    async def test_control_only_client_does_not_start_the_provider(self):
        fake = FakeProvider()
        transport = Gate()
        task = asyncio.create_task(self._session(transport, fake).run())
        try:
            ready = await self._ready(transport, _start(2))
            self.assertFalse(any(ready["capabilities"].values()))
            self.assertFalse(fake.started)
        finally:
            await transport.inbox.put(None)
            await task
        self.assertTrue(fake.closed)

    async def test_tool_result_failure_closes_the_socket(self):
        fake = FakeProvider(tool_error=True)
        transport = Gate()
        task = asyncio.create_task(self._session(transport, fake).run())
        await self._ready(transport)
        await fake.emit(VoiceEvent(
            kind="tool_call", payload={"id": "call-9", "name": "avi_ack", "args": {}},
        ))
        await task
        self.assertEqual(transport.closed, (1011, "Voice provider unavailable"))
        self.assertEqual(fake.tool_results, [])
        self.assertEqual(_text_types(transport.sent), ["session.ready"])


class GatewayRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_route_uses_the_provider_even_if_mock_media_is_set(self):
        uplink = b"\x11\x22\x33\x44"
        downlink = b"\x55\x66"
        fake = FakeProvider()
        fake.reply = downlink
        with voice_env(GEMINI_API_KEY=SECRET, GEMINI_LIVE_MODEL=MODEL, AVIP_MOCK_MEDIA="1"):
            with patch("avi.providers.gemini_live.GeminiLiveProvider", return_value=fake):
                async with app.router.lifespan_context(app):
                    async with connection() as (incoming, outgoing):
                        self.assertEqual((await outgoing.get())["type"], "websocket.accept")
                        await send(incoming, _start())
                        ready = json.loads((await outgoing.get())["text"])
                        self.assertEqual(ready["type"], "session.ready")
                        self.assertTrue(ready["capabilities"]["audio_input"])
                        self.assertTrue(ready["capabilities"]["audio_output"])
                        self.assertFalse(ready["capabilities"]["robot_actions"])
                        self.assertTrue(fake.started)
                        await send_frame(incoming, data=_uplink(uplink))
                        event = await asyncio.wait_for(outgoing.get(), 1)
                        frame = unpack_frame(event["bytes"])
                        self.assertEqual(frame.payload, downlink)
                        self.assertEqual(fake.audio, [uplink])
        self.assertTrue(fake.closed)
        self.assertNotIn(SECRET, json.dumps(ready))

    async def test_missing_model_stays_on_the_mock_mirror_and_does_not_construct_live(self):
        payload = b"\x10\x20"
        with voice_env(GEMINI_API_KEY=SECRET, AVIP_MOCK_MEDIA="1"):
            with patch("avi.providers.gemini_live.GeminiLiveProvider", side_effect=AssertionError("live")):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost"
                ) as client:
                    body = (await client.get("/health")).json()
                self.assertEqual(body["voice"], MODE_MOCK)
                self.assertEqual(body["voice_reason"], REASON_MODEL_UNCONFIGURED)
                self.assertNotIn(SECRET, json.dumps(body))
                async with app.router.lifespan_context(app):
                    async with connection() as (incoming, outgoing):
                        await outgoing.get()
                        await send(incoming, _start())
                        ready = json.loads((await outgoing.get())["text"])
                        self.assertTrue(ready["capabilities"]["audio_output"])
                        await send_frame(incoming, data=_uplink(payload))
                        event = await asyncio.wait_for(outgoing.get(), 1)
                        self.assertEqual(unpack_frame(event["bytes"]).payload, payload)

    async def test_unconfigured_route_does_not_construct_live(self):
        with voice_env():
            with patch("avi.providers.gemini_live.GeminiLiveProvider", side_effect=AssertionError("live")):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://localhost"
                ) as client:
                    body = (await client.get("/health")).json()
                self.assertEqual(body["voice"], MODE_NOT_CONFIGURED)
                self.assertEqual(body["voice_reason"], REASON_CREDENTIALS_ABSENT)
                async with app.router.lifespan_context(app):
                    async with connection() as (incoming, outgoing):
                        await outgoing.get()
                        await send(incoming, _start())
                        ready = json.loads((await outgoing.get())["text"])
                        self.assertFalse(any(ready["capabilities"].values()))


def pacer_backlog_limit() -> int:
    from avi.core.downlink import MAX_BACKLOG_BYTES
    return MAX_BACKLOG_BYTES


def _assert_speaker_plays(trace: list[tuple[float, bytes]]) -> None:
    """Play the paced frames on the browser SpeakerBuffer. Drops fail the test."""

    import subprocess
    payload = json.dumps([{"t": stamp, "hex": data.hex()} for stamp, data in trace])
    script = r"""
import {SpeakerBuffer} from "./clients/browser/voice.js";
import {readFileSync} from "node:fs";
const trace = JSON.parse(readFileSync(0, "utf8"));
let now = 0;
const buffer = new SpeakerBuffer({
  now: () => now,
  play() { return {ended: false, stop() {}}; },
});
let dropped = 0;
for (const item of trace) {
  now = item.t;
  const payload = Buffer.from(item.hex, "hex");
  if (!buffer.enqueue(payload)) dropped += 1;
}
if (dropped !== 0 || buffer.played !== trace.length) {
  console.error(JSON.stringify({
    dropped, played: buffer.played, frames: trace.length, depth: buffer.depthFrames(),
  }));
  process.exit(1);
}
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=payload,
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout or "speaker dropped paced audio")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _scrub(text, secret):
    if secret and secret in text:
        return text.replace(secret, "[redacted]")
    return text


def _require_live_credentials():
    key = os.environ.get("GEMINI_API_KEY")
    if not isinstance(key, str) or not key.strip():
        return None
    model = os.environ.get("GEMINI_LIVE_MODEL")
    if not isinstance(model, str) or not model.strip():
        raise AssertionError(
            "GEMINI_API_KEY is set but GEMINI_LIVE_MODEL is unset; "
            "refusing to choose a model"
        )
    return key


def _assert_downlink_frame(message):
    from avi.protocols.media import Direction as AudioDirection
    if not hasattr(message, "payload"):
        raise AssertionError("downlink was not an audio frame")
    payload = message.payload
    if type(payload) is not bytes or len(payload) < 2 or len(payload) % 2 != 0:
        raise AssertionError("downlink frame was empty or not pcm")
    if message.direction is not AudioDirection.AUDIO_DOWNLINK and message.direction != 2:
        raise AssertionError("downlink frame had the wrong direction")
    return payload


class LiveGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_silence_uplink_stays_open(self):
        """Silence is not a prompt. The socket must stay up. Audio is optional."""

        key = _require_live_credentials()
        if key is None:
            self.skipTest("GEMINI_API_KEY is unset; integration skipped")
        port = _free_port()
        log = tempfile.TemporaryFile(mode="w+")
        env = os.environ.copy()
        env.pop("AVIP_MOCK_MEDIA", None)
        env.pop("AVIP_LIVE_ELICIT", None)
        process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "avi.main:app",
                "--host", "127.0.0.1", "--port", str(port),
                "--ws-max-size", "16384", "--ws-max-queue", "16",
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
        )
        try:
            await _wait_health(port, process, log, key)
            from avip_client import AvipClient

            async with AvipClient(
                f"ws://127.0.0.1:{port}/ws/avip",
                request_id="req-live-silence",
                device_id="headless-sim-001",
                capabilities={"audio_input": True, "audio_output": True},
            ) as client:
                await asyncio.wait_for(client.start(), 45)
                await client.send_uplink(bytes(640))
                try:
                    message = await asyncio.wait_for(client.recv(), 2)
                except TimeoutError:
                    message = None
                if message is not None and not isinstance(message, dict):
                    _assert_downlink_frame(message)
            if process.poll() is not None:
                raise AssertionError("gateway exited after silence")
        except AssertionError:
            raise
        except Exception:
            log.seek(0)
            raise AssertionError(_scrub(log.read(), key) or "silence uplink failed") from None
        finally:
            _stop_process(process)
            log.close()

    async def test_configured_gateway_returns_speech(self):
        """Elicit one spoken word through /ws/avip and require a real frame."""

        key = _require_live_credentials()
        if key is None:
            self.skipTest("GEMINI_API_KEY is unset; integration skipped")
        port = _free_port()
        log = tempfile.TemporaryFile(mode="w+")
        env = os.environ.copy()
        env.pop("AVIP_MOCK_MEDIA", None)
        env["AVIP_LIVE_ELICIT"] = "Say the single word yes."
        process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "avi.main:app",
                "--host", "127.0.0.1", "--port", str(port),
                "--ws-max-size", "16384", "--ws-max-queue", "16",
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
        )
        try:
            await _wait_health(port, process, log, key)
            from avip_client import AvipClient

            async with AvipClient(
                f"ws://127.0.0.1:{port}/ws/avip",
                request_id="req-live-speech",
                device_id="headless-sim-001",
                capabilities={"audio_input": True, "audio_output": True},
            ) as client:
                ready = await asyncio.wait_for(client.start(), 45)
                if key in json.dumps(ready):
                    raise AssertionError("session.ready included a credential")
                payload = await _first_downlink(client, 30)
                if len(payload) < 2:
                    raise AssertionError("speech downlink was empty")
            log.seek(0)
            text = log.read()
            if key in text:
                raise AssertionError("gateway log included a credential")
            if process.poll() is not None:
                raise AssertionError("gateway exited after speech")
        except AssertionError:
            raise
        except Exception:
            log.seek(0)
            raise AssertionError(_scrub(log.read(), key) or "speech downlink failed") from None
        finally:
            _stop_process(process)
            log.close()

    async def test_live_interrupt_keeps_the_next_response(self):
        """Cancel an active generation, then hear the following one."""

        key = _require_live_credentials()
        if key is None:
            self.skipTest("GEMINI_API_KEY is unset; integration skipped")
        from avi.providers.gemini_live import GeminiLiveProvider

        provider = GeminiLiveProvider()
        transport = Gate()
        session = AvipSession(
            transport,
            SessionLimits(),
            AUDIO,
            provider=provider,
            telemetry_path="provider",
            telemetry_scenario="gemini-live",
        )
        task = asyncio.create_task(session.run())
        try:
            await transport.inbox.put(("text", _start()))
            ready = await asyncio.wait_for(transport.next_item(), 45)
            if ready.get("type") != "session.ready":
                raise AssertionError("live session was not ready")
            await provider._submit_text("Say the single word yes.")
            await _first_gateway_downlink(transport, 30)
            await provider.cancel_response()
            event = await _next_json(transport, 20)
            if event.get("type") != "response.interrupted" or event.get("generation") != 0:
                raise AssertionError("live interrupt was not signaled")
            if key in json.dumps(event):
                raise AssertionError("interrupt event included a credential")
            await provider._submit_text("Say the single word no.")
            await _first_gateway_downlink(transport, 30)
        except AssertionError:
            raise
        finally:
            await transport.inbox.put(None)
            await task
        self.assertEqual(provider._state, "closed")
        self.assertEqual(_provider_tasks(), [])


async def _first_downlink(client, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        message = await asyncio.wait_for(client.recv(), remaining)
        if isinstance(message, dict):
            if message.get("type") == "response.interrupted":
                continue
            raise AssertionError("unexpected control message before speech")
        return _assert_downlink_frame(message)
    raise AssertionError("no speech downlink")


async def _first_gateway_downlink(transport, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        item = await asyncio.wait_for(transport.next_item(), deadline - time.monotonic())
        if isinstance(item, dict):
            if item.get("type") == "response.interrupted":
                continue
            if item.get("type") == "session.ready":
                continue
            raise AssertionError("unexpected control message before speech")
        frame = unpack_frame(item)
        if len(frame.payload) < 2 or len(frame.payload) % 2 != 0:
            raise AssertionError("speech downlink was empty or not pcm")
        return frame.payload
    raise AssertionError("no speech downlink")


async def _next_json(transport, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        item = await asyncio.wait_for(transport.next_item(), deadline - time.monotonic())
        if isinstance(item, dict) and item.get("type") != "session.ready":
            return item
    raise AssertionError("no interrupt event")


def _stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def _wait_health(port, process, log, secret):
    deadline = time.monotonic() + 15
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.seek(0)
            raise AssertionError(_scrub(log.read(), secret) or "gateway exited")
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
            if response.status_code == 200:
                body = response.json()
                if body.get("voice") != "live":
                    raise AssertionError("health voice was not live")
                if secret and secret in response.text:
                    raise AssertionError("health included a credential")
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    log.seek(0)
    raise AssertionError(_scrub(log.read(), secret) or "gateway did not become healthy")
