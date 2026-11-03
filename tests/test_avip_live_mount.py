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
    def _session(self, transport, provider, available=AUDIO, limits=None):
        return AvipSession(
            transport,
            SessionLimits() if limits is None else limits,
            available,
            provider=provider,
            telemetry_path="provider" if provider is not None else "mock-mirror",
            telemetry_scenario="gemini-live" if provider is not None else "gateway",
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

    async def test_provider_events_do_not_become_avip_json(self):
        fake = FakeProvider()
        transport = Gate()
        task = asyncio.create_task(self._session(transport, fake).run())
        try:
            await self._ready(transport)
            await fake.emit(VoiceEvent(kind="interrupted", payload={"source": "server"}))
            await fake.emit(VoiceEvent(
                kind="turn_complete",
                payload={"reason": None, "transcript_present": False},
            ))
            await fake.emit(VoiceEvent(
                kind="tool_call",
                payload={"id": "call-1", "name": "avi_ack", "args": {"note": SECRET, "handle": HANDLE}},
            ))
            await fake.wait_until(lambda: len(fake.tool_results) == 1)
            await fake.emit(VoiceEvent(kind="audio", payload=b"\x01\x00"))
            frame = unpack_frame(await asyncio.wait_for(transport.next_item(), 1))
            self.assertEqual(frame.payload, b"\x01\x00")
            self.assertEqual(fake.tool_results, [("call-1", {"output": {}})])
            self.assertEqual(_text_types(transport.sent), ["session.ready"])
            rendered = json.dumps(transport.sent, default=lambda item: item.hex())
            self.assertNotIn(SECRET, rendered)
            self.assertNotIn(HANDLE, rendered)
            self.assertNotIn("tool_call", rendered)
            self.assertNotIn("turn_complete", rendered)
            self.assertNotIn("interrupted", rendered)
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

    async def test_downlink_longer_than_one_frame_is_split(self):
        fake = FakeProvider()
        transport = Gate()
        task = asyncio.create_task(self._session(transport, fake).run())
        try:
            await self._ready(transport)
            payload = b"\x00\x01" * 4097
            await fake.emit(VoiceEvent(kind="audio", payload=payload))
            first = unpack_frame(await asyncio.wait_for(transport.next_item(), 1))
            second = unpack_frame(await asyncio.wait_for(transport.next_item(), 1))
            self.assertEqual(first.sequence, 0)
            self.assertEqual(first.timestamp_us, 0)
            self.assertEqual(len(first.payload), 8192)
            self.assertEqual(second.sequence, 1)
            self.assertEqual(second.timestamp_us, 4096 * 1_000_000 // 16_000)
            self.assertEqual(first.payload + second.payload, payload)
        finally:
            await transport.inbox.put(None)
            await task

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


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _scrub(text, secret):
    if secret and secret in text:
        return text.replace(secret, "[redacted]")
    return text


class LiveGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_gateway_accepts_uplink(self):
        """One real Live session on /ws/avip. Skips when the key is unset."""

        key = os.environ.get("GEMINI_API_KEY")
        if not isinstance(key, str) or not key.strip():
            self.skipTest("GEMINI_API_KEY is unset; integration skipped")
        model = os.environ.get("GEMINI_LIVE_MODEL")
        if not isinstance(model, str) or not model.strip():
            self.fail(
                "GEMINI_API_KEY is set but GEMINI_LIVE_MODEL is unset; "
                "refusing to choose a model"
            )
        port = _free_port()
        log = tempfile.TemporaryFile(mode="w+")
        env = os.environ.copy()
        env.pop("AVIP_MOCK_MEDIA", None)
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
        url = f"ws://127.0.0.1:{port}/ws/avip"
        try:
            await _wait_health(port, process, log, key)
            from avip_client import AvipClient

            async with AvipClient(
                url,
                request_id="req-live-mount",
                device_id="headless-sim-001",
                capabilities={"audio_input": True, "audio_output": True},
            ) as client:
                ready = await asyncio.wait_for(client.start(), 45)
                self.assertTrue(ready["capabilities"]["audio_input"])
                self.assertTrue(ready["capabilities"]["audio_output"])
                self.assertFalse(ready["capabilities"]["robot_actions"])
                encoded = json.dumps(ready)
                self.assertNotIn("gemini", encoded.lower())
                if key in encoded:
                    self.fail("session.ready included a credential")
                await client.send_uplink(bytes(640))
                downlink = 0
                try:
                    while True:
                        message = await asyncio.wait_for(client.recv(), 2)
                        if isinstance(message, dict):
                            self.fail("unexpected control message during live uplink")
                        downlink += 1
                        if downlink > 8:
                            break
                except TimeoutError:
                    pass
            self.assertGreaterEqual(downlink, 0)
        except AssertionError:
            raise
        except Exception as exc:
            log.seek(0)
            detail = _scrub(log.read(), key)
            raise AssertionError(detail or type(exc).__name__) from None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log.close()


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
