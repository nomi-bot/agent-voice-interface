"""VoiceProvider contract for the Gemini Live adapter. No network by default."""

import asyncio
from pathlib import Path
import traceback
import unittest

from avi.main import resolve_browser_dir
from avi.providers.gemini_live import (
    CANCEL_SIGNAL,
    TOOL_NAME,
    GeminiLiveProvider,
    ProviderNotConfigured,
    ProviderSessionError,
    live_connect_config,
    run_duplex_roundtrip,
    run_tool_handshake,
)
from avi.providers.gemini_live_spike import (
    INPUT_SAMPLE_RATE_HZ,
    OUTPUT_SAMPLE_RATE_HZ,
    ModelUnconfigured,
    SmokeStatus,
    plan_smoke,
)
from avi.providers.resample import StreamingResampler


ROOT = Path(__file__).resolve().parents[1]


class _Message:
    def __init__(self, **kwargs):
        self.server_content = kwargs.get("server_content")
        self.go_away = kwargs.get("go_away")
        self.tool_call = kwargs.get("tool_call")
        self.tool_call_cancellation = kwargs.get("tool_call_cancellation")
        self.session_resumption_update = kwargs.get("session_resumption_update")
        self.data = kwargs.get("data")


class _Blob:
    def __init__(self, data, mime_type):
        self.data = data
        self.mime_type = mime_type


class _Part:
    def __init__(self, data, mime_type):
        self.inline_data = _Blob(data, mime_type)


class _Content:
    def __init__(self, parts, **kwargs):
        self.model_turn = type("Turn", (), {"parts": parts})()
        self.interrupted = kwargs.get("interrupted", False)
        self.turn_complete = kwargs.get("turn_complete", False)
        self.turn_complete_reason = kwargs.get("turn_complete_reason")
        self.generation_complete = kwargs.get("generation_complete", False)
        self.interaction_status = kwargs.get("interaction_status")
        self.output_transcription = kwargs.get("output_transcription")


class FakeSession:
    def __init__(self):
        self.audio = []
        self.tools = []
        self.turns = []
        self.closed = False
        self.block_send = None
        self.fail_tool: BaseException | None = None
        self.fail_content: BaseException | None = None
        self.fail_audio: BaseException | None = None
        self._queue = asyncio.Queue()

    async def send_realtime_input(self, *, audio=None, **kwargs):
        if self.fail_audio is not None:
            raise self.fail_audio
        if self.block_send is not None:
            await self.block_send.wait()
        self.audio.append(audio)

    async def send_tool_response(self, *, function_responses):
        if self.fail_tool is not None:
            raise self.fail_tool
        self.tools.append(function_responses)

    async def send_client_content(self, *, turns=None, turn_complete=True):
        if self.fail_content is not None:
            raise self.fail_content
        self.turns.append((turns, turn_complete))

    def receive(self):
        return self._receive()

    async def _receive(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item
            content = getattr(item, "server_content", None)
            if content is not None and getattr(content, "turn_complete", False):
                return

    async def close(self):
        self.closed = True
        await self._queue.put(None)

    async def push(self, message):
        await self._queue.put(message)


def _pcm(value: int, samples: int) -> bytes:
    word = int(value).to_bytes(2, "little", signed=True)
    return word * samples


def _audio_message(pcm: bytes, rate: int = 24000, **content):
    return _Message(
        server_content=_Content(
            [_Part(pcm, f"audio/pcm;rate={rate}")],
            **content,
        ),
        data=b"",
    )


def _provider(session, **kwargs):
    async def connect():
        return session

    return GeminiLiveProvider(env={}, connector=connect, **kwargs)


async def _events_until_quiet(provider, limit=8):
    found = []
    async for event in provider.events():
        found.append(event)
        if len(found) >= limit:
            break
        if event.kind in {"turn_complete", "error", "interrupted", "tool_call"}:
            break
    return found


class ResamplerTests(unittest.TestCase):
    def test_chunks_match_one_shot_for_three_seconds(self):
        samples = 3 * OUTPUT_SAMPLE_RATE_HZ
        pcm = b"".join(
            int(10000 * ((index % 17) - 8) / 8).to_bytes(2, "little", signed=True)
            for index in range(samples)
        )
        once = StreamingResampler(OUTPUT_SAMPLE_RATE_HZ, INPUT_SAMPLE_RATE_HZ)
        whole = once.push(pcm)
        split = StreamingResampler(OUTPUT_SAMPLE_RATE_HZ, INPUT_SAMPLE_RATE_HZ)
        parts = []
        chunk = 960
        for offset in range(0, len(pcm), chunk):
            parts.append(split.push(pcm[offset : offset + chunk]))
        self.assertEqual(b"".join(parts), whole)
        self.assertEqual(len(whole) // 2, 3 * INPUT_SAMPLE_RATE_HZ)

    def test_equal_rates_copy_bytes(self):
        resampler = StreamingResampler(INPUT_SAMPLE_RATE_HZ, INPUT_SAMPLE_RATE_HZ)
        payload = _pcm(7, 4)
        self.assertEqual(resampler.push(payload), payload)
        self.assertEqual(resampler.push(b""), b"")

    def test_reset_drops_the_tail(self):
        resampler = StreamingResampler(OUTPUT_SAMPLE_RATE_HZ, INPUT_SAMPLE_RATE_HZ)
        resampler.push(_pcm(1000, 3))
        resampler.reset()
        produced = resampler.push(_pcm(0, 48))
        self.assertTrue(produced)
        self.assertNotIn((1000).to_bytes(2, "little", signed=True), produced)

    def test_odd_length_is_rejected(self):
        resampler = StreamingResampler(OUTPUT_SAMPLE_RATE_HZ, INPUT_SAMPLE_RATE_HZ)
        with self.assertRaises(ValueError):
            resampler.push(b"\x00")


class ProviderContractTests(unittest.IsolatedAsyncioTestCase):
    def test_structural_port_and_gateway_stay_separate(self):
        provider = GeminiLiveProvider(env={})
        for name in (
            "start",
            "send_audio",
            "events",
            "tool_result",
            "cancel_response",
            "close",
        ):
            self.assertTrue(callable(getattr(provider, name)))
        self.assertTrue(provider.capabilities.full_duplex)
        source = (ROOT / "src" / "avi" / "main.py").read_text()
        self.assertNotIn("GeminiLiveProvider", source)
        self.assertNotIn("GEMINI_API_KEY", source)

    async def test_missing_key_does_not_connect(self):
        provider = GeminiLiveProvider(env={"GOOGLE_API_KEY": "other-secret"})
        with self.assertRaises(ProviderNotConfigured):
            await provider.start()
        self.assertIsNone(provider._reader)
        self.assertIsNone(provider._sender)
        self.assertNotIn("other-secret", repr(provider))
        await provider.close()

    async def test_key_without_model_fails_closed(self):
        provider = GeminiLiveProvider(env={"GEMINI_API_KEY": "present"})
        with self.assertRaises(ModelUnconfigured) as caught:
            await provider.start()
        self.assertNotIn("present", str(caught.exception))
        await provider.close()

    async def test_connect_failure_redacts_and_leaves_no_tasks(self):
        secret = "super-secret-value"

        async def boom():
            raise RuntimeError(f"{secret} rejected")

        provider = GeminiLiveProvider(
            env={"GEMINI_API_KEY": secret, "GEMINI_LIVE_MODEL": "operator-selected-model"},
            connector=boom,
        )
        with self.assertRaises(ProviderSessionError) as caught:
            await provider.start()
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))
        self.assertIsNone(provider._reader)
        self.assertIsNone(provider._sender)
        names = {task.get_name() for task in asyncio.all_tasks()}
        self.assertFalse(any(name.startswith("avi-live") for name in names))
        self.assertNotIn(secret, repr(provider))
        await provider.close()

    async def test_close_during_connect_closes_the_session(self):
        session = FakeSession()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow():
            started.set()
            await release.wait()
            return session

        provider = GeminiLiveProvider(env={}, connector=slow)
        opening = asyncio.create_task(provider.start())
        await started.wait()
        await provider.close()
        release.set()
        with self.assertRaises(ProviderSessionError):
            await opening
        self.assertTrue(session.closed)
        self.assertEqual(provider._state, "closed")
        await provider.close()

    async def test_downlink_is_resampled_to_16k_and_uplink_is_16k(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        uplink = _pcm(0, 320)
        await provider.send_audio(uplink)
        await provider._wait_uplink_idle()
        self.assertEqual(provider.sent_uplink_bytes, len(uplink))
        self.assertEqual(session.audio[0]["mime_type"], "audio/pcm;rate=16000")
        self.assertEqual(session.audio[0]["data"], uplink)
        await session.push(_audio_message(_pcm(1000, 480), rate=24000, turn_complete=True))
        events = await _events_until_quiet(provider)
        audio = [event for event in events if event.kind == "audio"]
        self.assertEqual(len(audio), 1)
        self.assertEqual(len(audio[0].payload), 640)
        self.assertEqual(audio[0].payload, _pcm(1000, 320))
        self.assertEqual(events[-1].kind, "turn_complete")
        await provider.close()
        self.assertTrue(session.closed)

    async def test_parts_audio_is_not_taken_from_empty_mirrored_data(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        message = _audio_message(_pcm(5, 480), turn_complete=True)
        message.data = b""
        await session.push(message)
        events = await _events_until_quiet(provider)
        audio = [event for event in events if event.kind == "audio"]
        self.assertEqual(len(audio), 1)
        self.assertEqual(len(audio[0].payload), 640)
        await provider.close()

    async def test_uplink_queue_drops_when_full(self):
        session = FakeSession()
        session.block_send = asyncio.Event()
        provider = _provider(session, max_uplink_chunks=1)
        chunk = _pcm(1, 4)
        await provider.start()
        await asyncio.sleep(0)
        await provider.send_audio(chunk)
        await asyncio.sleep(0)
        await provider.send_audio(chunk)
        await provider.send_audio(chunk)
        self.assertEqual(provider.dropped_uplink_chunks, 1)
        session.block_send.set()
        await provider._wait_uplink_idle()
        self.assertEqual(provider.sent_uplink_bytes, len(chunk) * 2)
        await provider.close()

    async def test_audio_events_are_bounded(self):
        session = FakeSession()
        provider = _provider(session, max_audio_events=1)
        await provider.start()
        await session.push(_audio_message(_pcm(3, 480)))
        await session.push(_audio_message(_pcm(4, 480)))
        for _ in range(20):
            if provider.dropped_audio_events == 1:
                break
            await asyncio.sleep(0)
        self.assertEqual(provider.dropped_audio_events, 1)
        await session.push(_audio_message(b"", rate=24000, turn_complete=True))
        # An empty part does not emit audio. Finish with an explicit turn.
        events = []
        async for event in provider.events():
            events.append(event)
            if event.kind == "turn_complete":
                break
        audio = [event for event in events if event.kind == "audio"]
        self.assertEqual(len(audio), 1)
        await provider.close()

    async def test_interrupted_audio_is_not_emitted(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        await session.push(
            _audio_message(_pcm(9, 480), interrupted=True, turn_complete=True)
        )
        events = await _events_until_quiet(provider)
        self.assertNotIn("audio", [event.kind for event in events])
        self.assertEqual(events[0].kind, "interrupted")
        self.assertEqual(events[0].payload, {"source": "server"})
        await provider.close()

    async def test_cancel_response_interrupts_without_an_agent_run(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        await session.push(_audio_message(_pcm(8, 480)))
        for _ in range(20):
            if provider._events._audio:
                break
            await asyncio.sleep(0)
        await provider.cancel_response()
        self.assertEqual(session.turns, [(None, True)])
        self.assertGreaterEqual(provider.flushed_audio_events, 1)
        events = await _events_until_quiet(provider)
        self.assertEqual(events[0].kind, "interrupted")
        self.assertEqual(events[0].payload["source"], "cancel")
        self.assertEqual(events[0].payload["signal"], CANCEL_SIGNAL)
        self.assertNotIn("hermes", repr(events))
        await provider.close()

    async def test_cancel_drops_delayed_audio_until_the_next_turn(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        iterator = provider.events()
        stale = _pcm(2000, 480)
        fresh = _pcm(7, 480)
        await session.push(_audio_message(stale))
        first = await anext(iterator)
        self.assertEqual(first.kind, "audio")
        await provider.cancel_response()
        interrupted = await anext(iterator)
        self.assertEqual(interrupted.kind, "interrupted")
        await session.push(_audio_message(stale))
        await session.push(_Message(server_content=_Content([], turn_complete=True)))
        boundary = await anext(iterator)
        self.assertEqual(boundary.kind, "turn_complete")
        self.assertEqual(provider.suppressed_audio_chunks, 1)
        await session.push(_audio_message(fresh, turn_complete=True))
        nxt = await anext(iterator)
        self.assertEqual(nxt.kind, "audio")
        self.assertTrue(nxt.payload.startswith((7).to_bytes(2, "little", signed=True)))
        self.assertFalse(nxt.payload.startswith((2000).to_bytes(2, "little", signed=True)))
        done = await anext(iterator)
        self.assertEqual(done.kind, "turn_complete")
        await provider.close()

    async def test_idle_cancel_does_not_drop_the_next_turn(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        iterator = provider.events()
        await provider.cancel_response()
        await provider.cancel_response()
        self.assertEqual(session.turns, [])
        fresh = _pcm(4, 480)
        await session.push(_audio_message(fresh, turn_complete=True))
        first = await anext(iterator)
        self.assertEqual(first.kind, "audio")
        self.assertTrue(first.payload.startswith((4).to_bytes(2, "little", signed=True)))
        boundary = await anext(iterator)
        self.assertEqual(boundary.kind, "turn_complete")
        await provider.close()

    async def test_repeated_cancel_does_not_hold_the_following_turn(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        iterator = provider.events()
        await session.push(_audio_message(_pcm(3, 480)))
        first = await anext(iterator)
        self.assertEqual(first.kind, "audio")
        await provider.cancel_response()
        await provider.cancel_response()
        self.assertEqual(session.turns, [(None, True)])
        interrupted = await anext(iterator)
        self.assertEqual(interrupted.kind, "interrupted")
        await session.push(_Message(server_content=_Content([], turn_complete=True)))
        boundary = await anext(iterator)
        self.assertEqual(boundary.kind, "turn_complete")
        fresh = _pcm(11, 480)
        await session.push(_audio_message(fresh, turn_complete=True))
        nxt = await anext(iterator)
        self.assertEqual(nxt.kind, "audio")
        self.assertTrue(nxt.payload.startswith((11).to_bytes(2, "little", signed=True)))
        await provider.close()

    async def test_turn_boundary_does_not_keep_the_previous_tail(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        iterator = provider.events()
        marker = (9000).to_bytes(2, "little", signed=True)
        await session.push(_audio_message(marker, turn_complete=True))
        first = await anext(iterator)
        self.assertEqual(first.kind, "turn_complete")
        await session.push(_audio_message(_pcm(0, 2), turn_complete=True))
        second = await anext(iterator)
        self.assertEqual(second.kind, "audio")
        self.assertFalse(second.payload.startswith(marker))
        self.assertEqual(second.payload[:2], (0).to_bytes(2, "little", signed=True))
        third = await anext(iterator)
        self.assertEqual(third.kind, "turn_complete")
        await provider.close()

    async def test_session_errors_redact_the_key_before_shutdown(self):
        secret = "super-secret-value"
        session = FakeSession()
        session.fail_tool = RuntimeError(f"tool rejected {secret}")
        session.fail_content = RuntimeError(f"cancel rejected {secret}")
        session.fail_audio = RuntimeError(f"audio rejected {secret}")

        async def connect():
            return session

        provider = GeminiLiveProvider(
            env={"GEMINI_API_KEY": secret, "GEMINI_LIVE_MODEL": "operator-selected-model"},
            connector=connect,
        )
        await provider.start()
        call = type("Call", (), {"id": "call-9", "name": TOOL_NAME, "args": {}})()
        await session.push(
            _Message(tool_call=type("Tool", (), {"function_calls": [call]})())
        )
        iterator = provider.events()
        self.assertEqual((await anext(iterator)).kind, "tool_call")
        with self.assertLogs("avi.providers.gemini_live", level="WARNING") as captured:
            with self.assertRaises(ProviderSessionError) as caught:
                await provider.tool_result("call-9", {"ok": True})
        self._assert_secret_absent(secret, caught.exception, captured.output)
        await provider.close()

        session = FakeSession()
        session.fail_content = RuntimeError(f"cancel rejected {secret}")

        async def connect_cancel():
            return session

        provider = GeminiLiveProvider(
            env={"GEMINI_API_KEY": secret, "GEMINI_LIVE_MODEL": "operator-selected-model"},
            connector=connect_cancel,
        )
        await provider.start()
        iterator = provider.events()
        await session.push(_audio_message(_pcm(2, 480)))
        self.assertEqual((await anext(iterator)).kind, "audio")
        with self.assertLogs("avi.providers.gemini_live", level="WARNING") as captured:
            with self.assertRaises(ProviderSessionError) as caught:
                await provider.cancel_response()
        self._assert_secret_absent(secret, caught.exception, captured.output)
        await provider.close()

        session = FakeSession()
        session.fail_audio = RuntimeError(f"audio rejected {secret}")

        async def connect_audio():
            return session

        provider = GeminiLiveProvider(
            env={"GEMINI_API_KEY": secret, "GEMINI_LIVE_MODEL": "operator-selected-model"},
            connector=connect_audio,
        )
        await provider.start()
        iterator = provider.events()
        with self.assertLogs("avi.providers.gemini_live", level="WARNING") as captured:
            await provider.send_audio(_pcm(0, 4))
            event = await anext(iterator)
        self.assertEqual(event.kind, "error")
        rendered = str(event.payload) + "\n" + "\n".join(captured.output)
        self.assertNotIn(secret, rendered)
        self.assertIn("[redacted]", rendered)
        await provider.close()

    def _assert_secret_absent(self, secret, error, logs):
        rendered = "\n".join(
            [
                str(error),
                repr(error),
                "".join(traceback.format_exception(error)),
                "" if error.__cause__ is None else str(error.__cause__),
                "" if error.__context__ is None else str(error.__context__),
                "\n".join(logs),
            ]
        )
        self.assertNotIn(secret, rendered)
        self.assertIn("[redacted]", str(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    async def test_tool_round_trip_uses_explicit_scheduling(self):
        session = FakeSession()
        provider = _provider(session, tool_behavior="NON_BLOCKING")
        await provider.start()
        call = type("Call", (), {"id": "call-1", "name": TOOL_NAME, "args": {}})()
        await session.push(
            _Message(tool_call=type("Tool", (), {"function_calls": [call]})())
        )
        events = await _events_until_quiet(provider)
        self.assertEqual(events[0].kind, "tool_call")
        self.assertEqual(events[0].payload["id"], "call-1")
        self.assertEqual(events[0].payload["name"], TOOL_NAME)
        await provider.tool_result("call-1", {"ok": True})
        self.assertEqual(session.tools[0]["scheduling"], "WHEN_IDLE")
        self.assertEqual(session.tools[0]["response"], {"output": {"ok": True}})
        self.assertNotIn("call-1", provider._pending_tools)
        await provider.close()

    async def test_blocking_tool_response_omits_scheduling(self):
        session = FakeSession()
        provider = _provider(session, tool_behavior="BLOCKING")
        await provider.start()
        call = type("Call", (), {"id": "call-2", "name": TOOL_NAME, "args": {}})()
        await session.push(
            _Message(tool_call=type("Tool", (), {"function_calls": [call]})())
        )
        await _events_until_quiet(provider)
        await provider.tool_result("call-2", {"error": "nope"})
        self.assertNotIn("scheduling", session.tools[0])
        self.assertEqual(session.tools[0]["response"], {"error": "nope"})
        await provider.close()

    async def test_unknown_and_oversized_tool_results_are_rejected(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        with self.assertRaises(ValueError):
            await provider.tool_result("missing", {"ok": True})
        with self.assertRaises(TypeError):
            await provider.tool_result("missing", ["nope"])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            await provider.tool_result("missing", {"output": "x" * 20_000})
        self.assertEqual(session.tools, [])
        await provider.close()

    async def test_tool_cancellation_forgets_the_id(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        iterator = provider.events()
        call = type("Call", (), {"id": "call-3", "name": TOOL_NAME, "args": {}})()
        await session.push(
            _Message(tool_call=type("Tool", (), {"function_calls": [call]})())
        )
        first = await anext(iterator)
        self.assertEqual(first.kind, "tool_call")
        await session.push(
            _Message(
                tool_call_cancellation=type("Cancel", (), {"ids": ["call-3"]})()
            )
        )
        second = await anext(iterator)
        self.assertEqual(second.kind, "error")
        self.assertEqual(second.payload["code"], "tool_call_cancelled")
        with self.assertRaises(ValueError):
            await provider.tool_result("call-3", {"ok": True})
        await provider.close()

    async def test_go_away_closes_without_keeping_a_resumption_handle(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        handle = "handle-secret"
        await session.push(
            _Message(
                go_away=type("Away", (), {"time_left": "10s"})(),
                session_resumption_update=type(
                    "Update", (), {"resumable": True, "new_handle": handle}
                )(),
            )
        )
        events = []
        async for event in provider.events():
            events.append(event)
        self.assertEqual(events[0].payload["code"], "go_away")
        self.assertTrue(session.closed)
        self.assertEqual(provider.resumption_updates, 1)
        rendered = repr(provider) + repr(events)
        self.assertNotIn(handle, rendered)
        for value in provider.__dict__.values():
            self.assertNotIn(handle, repr(value))
        await provider.close()

    async def test_sample_rate_change_stops_the_session(self):
        session = FakeSession()
        provider = _provider(session)
        await provider.start()
        await session.push(_audio_message(_pcm(1, 480), rate=24000))
        await session.push(_audio_message(_pcm(1, 320), rate=16000))
        events = []
        async for event in provider.events():
            events.append(event)
        self.assertIn("sample_rate_changed", [event.payload.get("code") for event in events if isinstance(event.payload, dict)])
        self.assertTrue(session.closed)

    async def test_send_audio_rejects_bad_payloads_and_a_closed_provider(self):
        session = FakeSession()
        provider = _provider(session)
        with self.assertRaises(RuntimeError):
            await provider.send_audio(_pcm(0, 2))
        await provider.start()
        with self.assertRaises(ValueError):
            await provider.send_audio(b"")
        with self.assertRaises(ValueError):
            await provider.send_audio(b"\x00")
        with self.assertRaises(ValueError):
            await provider.send_audio(b"\x00\x00" * 20_000)
        with self.assertRaises(RuntimeError):
            await provider.start()
        consumer = provider.events()
        with self.assertRaises(RuntimeError):
            provider.events()
        await provider.close()
        self.assertIsNone(await anext(consumer, None))

    def test_live_config_sets_behavior_and_leaves_resumption_unset(self):
        from google.genai import types

        blocking = live_connect_config("BLOCKING").model_dump(exclude_none=True)
        declaration = blocking["tools"][0]["function_declarations"][0]
        self.assertEqual(declaration["name"], TOOL_NAME)
        self.assertEqual(declaration["behavior"], types.Behavior.BLOCKING)
        self.assertNotIn("session_resumption", blocking)
        self.assertNotIn("thinking_config", blocking)
        self.assertNotIn("enable_affective_dialog", blocking)
        self.assertNotIn("proactivity", blocking)
        nonblocking = live_connect_config("NON_BLOCKING").model_dump(exclude_none=True)
        behavior = nonblocking["tools"][0]["function_declarations"][0]["behavior"]
        self.assertEqual(behavior, types.Behavior.NON_BLOCKING)
        with self.assertRaises(ValueError):
            live_connect_config("UNSPECIFIED")
        response = types.FunctionResponse.model_validate(
            {
                "id": "call-1",
                "name": TOOL_NAME,
                "response": {"output": {"ok": True}},
                "scheduling": "WHEN_IDLE",
            }
        )
        self.assertEqual(response.scheduling, types.FunctionResponseScheduling.WHEN_IDLE)

    def test_packaged_browser_dir_wins_when_complete(self):
        import tempfile

        names = (
            "voice.html",
            "avip-client.js",
            "pcm.js",
            "voice.js",
            "telemetry.js",
            "capture-worklet.js",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packaged = root / "packaged"
            source = root / "source"
            packaged.mkdir()
            source.mkdir()
            with self.assertRaises(FileNotFoundError):
                resolve_browser_dir(packaged, source)
            for name in names:
                (source / name).write_text("source", encoding="utf-8")
            self.assertEqual(resolve_browser_dir(packaged, source), source)
            for name in names:
                (packaged / name).write_text("packaged", encoding="utf-8")
            self.assertEqual(resolve_browser_dir(packaged, source), packaged)

    async def test_live_duplex_and_tool_handshake_when_configured(self):
        import os

        try:
            plan = plan_smoke(os.environ)
        except ModelUnconfigured as exc:
            self.fail(str(exc))
        if plan.status == SmokeStatus.SKIPPED:
            self.skipTest("GEMINI_API_KEY unset")
        duplex = await run_duplex_roundtrip(os.environ)
        self.assertEqual(duplex.status, "passed", duplex.reason)
        self.assertGreater(duplex.uplink_bytes, 0)
        self.assertGreater(duplex.downlink_bytes, 0)
        self.assertIsNotNone(duplex.first_audio_ms)
        self.assertTrue(duplex.cancel_accepted)
        self.assertEqual(duplex.cancel_signal, CANCEL_SIGNAL)
        handshake = await run_tool_handshake(os.environ)
        self.assertEqual(handshake.status, "passed", handshake.reason)
        behaviors = {item.behavior: item for item in handshake.behaviors}
        self.assertEqual(set(behaviors), {"BLOCKING", "NON_BLOCKING"})
        self.assertTrue(behaviors["NON_BLOCKING"].saw_tool_call)
        self.assertTrue(behaviors["BLOCKING"].saw_tool_call)
        self.assertEqual(behaviors["NON_BLOCKING"].scheduling, "WHEN_IDLE")
        self.assertIsNone(behaviors["BLOCKING"].scheduling)
        rendered = repr(duplex) + repr(handshake)
        self.assertNotIn("handle", rendered.lower())


class BrowserAssetPackagingTests(unittest.TestCase):
    def test_wheel_contains_the_voice_assets(self):
        import tempfile
        import zipfile

        from hatchling.builders.wheel import WheelBuilder

        with tempfile.TemporaryDirectory() as tmp:
            builder = WheelBuilder(str(ROOT))
            wheel_path = Path(next(builder.build(directory=tmp, versions=["standard"])))
            with zipfile.ZipFile(wheel_path) as archive:
                names = set(archive.namelist())
            for asset in (
                "voice.html",
                "avip-client.js",
                "pcm.js",
                "voice.js",
                "telemetry.js",
                "capture-worklet.js",
            ):
                self.assertIn(f"avi/_browser/{asset}", names)

    def test_installed_wheel_serves_voice_outside_the_source_tree(self):
        import os
        import socket
        import subprocess
        import sys
        import tempfile
        import time
        import urllib.request

        from hatchling.builders.wheel import WheelBuilder

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = WheelBuilder(str(ROOT))
            wheel = Path(next(builder.build(directory=str(root / "dist"), versions=["standard"])))
            target = root / "pkg"
            installed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--target",
                    str(target),
                    str(wheel),
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=root,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import avi.main; print(avi.main.__file__)",
                ],
                check=False,
                capture_output=True,
                text=True,
                cwd=root,
                env={**os.environ, "PYTHONPATH": str(target)},
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            location = probe.stdout.strip()
            self.assertIn(str(target), location)
            self.assertNotIn(str(ROOT / "src"), location)
            port = _free_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "avi.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=root,
                env={**os.environ, "PYTHONPATH": str(target)},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_http(port, process)
                paths = (
                    "/voice",
                    "/voice/voice.js",
                    "/voice/pcm.js",
                    "/voice/avip-client.js",
                    "/voice/telemetry.js",
                    "/voice/capture-worklet.js",
                )
                bodies = {}
                for path in paths:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=5
                    ) as response:
                        self.assertEqual(response.status, 200, path)
                        bodies[path] = response.read()
                        self.assertTrue(bodies[path], path)
                self.assertIn(b"./telemetry.js", bodies["/voice/voice.js"])
                self.assertIn(b"VoiceTelemetry", bodies["/voice/telemetry.js"])
                self.assertIn(b"/voice/voice.js", bodies["/voice"])
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(port, process):
    import time
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + 15
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read().decode() if process.stdout else ""
            raise AssertionError(output or "gateway exited")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.05)
    output = ""
    if process.stdout:
        process.terminate()
    raise AssertionError(output or "gateway did not become healthy")


if __name__ == "__main__":
    unittest.main()
