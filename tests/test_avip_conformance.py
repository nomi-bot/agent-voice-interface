"""Both mock clients speak AVIP on one /ws/avip gateway."""
import asyncio
import json
from pathlib import Path
import re
import time
import unittest
from unittest.mock import patch

from avi.core.session import SessionLimits
from avi.main import app
from avi.protocols import avip, media
from avi.protocols.media import Direction, MediaFrame, pack_frame
from avip_clients import (
    HANDSHAKE,
    MEDIA,
    BrowserShapedClient,
    HeadlessPythonClient,
    both_clients,
    direction_of,
    payload_of,
    sequence_of,
    timestamp_of,
)
from test_avip_websocket import connection, send, send_frame


TOKEN = "LEAK9f3a"
DISABLED = {
    "audio_input": False,
    "audio_output": False,
    "camera": False,
    "display": False,
    "robot_actions": False,
}
DOWNLINK = MEDIA["directions"]["audio_downlink"]
JS_CLIENT = Path(__file__).parent / "fixtures" / "browser-avip-client.js"
# 30.0 s of the M0 profile is 1500 frames × 20 ms at 16 kHz pcm_s16le.
# Socket tests submit those frames as fast as each mirror echo returns,
# which is faster than the media clock. The equivalent media duration is
# 30 seconds; the assertion is that the queue does not stick at depth 8.
SUSTAINED_MEDIA_US = 30_000_000
SUSTAINED_FRAMES = SUSTAINED_MEDIA_US // media.NOMINAL_CHUNK_US


def wire_error(code, field):
    return {"protocol": "avip/1", "type": "error", "code": code, "field": field}


def duration_us(payload: bytes) -> int:
    """Gateway mirror clock: samples at 16 kHz, integer microseconds."""
    samples = len(payload) // media.SAMPLE_WIDTH_BYTES
    return samples * 1_000_000 // media.SAMPLE_RATE_HZ


def mirror(sequence: int, timestamp_us: int, payload: bytes) -> bytes:
    return pack_frame(MediaFrame(
        Direction.AUDIO_DOWNLINK, sequence, timestamp_us, payload
    ))


class ConformanceTests(unittest.IsolatedAsyncioTestCase):
    def test_browser_bytes_match_the_shared_framing_module(self):
        browser = BrowserShapedClient()
        headless = HeadlessPythonClient()
        seen = 0
        for vector in MEDIA["vectors"]:
            if vector["direction"] != "audio_uplink":
                continue
            payload = bytes.fromhex(vector["payload_hex"])
            packed = browser.pack_uplink(vector["sequence"], vector["timestamp_us"], payload)
            self.assertEqual(
                packed,
                headless.pack_uplink(vector["sequence"], vector["timestamp_us"], payload),
            )
            self.assertEqual(packed, bytes.fromhex(vector["frame_hex"]))
            self.assertEqual(media.unpack_frame(packed).payload, payload)
            seen += 1
        self.assertGreaterEqual(seen, 1)
        nominal = bytes(MEDIA["nominal_payload_bytes"])
        self.assertEqual(len(nominal), 640)
        self.assertEqual(browser.pack_uplink(0, 0, nominal), headless.pack_uplink(0, 0, nominal))
        script = JS_CLIENT.read_text()
        self.assertIn(MEDIA["vectors"][0]["frame_hex"], script)
        self.assertIn("setUint32(8, sequence, true)", script)
        self.assertIn("setBigUint64(12, BigInt(timestampUs), true)", script)
        self.assertIn("[0x41, 0x56, 0x49, 0x50]", script)
        self.assertIsNotNone(re.search(r"device_id: \"browser-demo-001\"", script))

    async def test_both_clients_round_trip_on_one_gateway(self):
        browser, headless = both_clients()
        golden = bytes.fromhex(MEDIA["vectors"][0]["payload_hex"])
        nominal = bytes([i % 256 for i in range(MEDIA["nominal_payload_bytes"])])
        with self._audio_available():
            async with app.router.lifespan_context(app):
                async with connection() as (browser_in, browser_out):
                    async with connection() as (headless_in, headless_out):
                        ready = await asyncio.gather(
                            self._handshake(browser, browser_in, browser_out),
                            self._handshake(headless, headless_in, headless_out),
                        )
                        self.assertEqual(app.state.active_sessions, 2)
                        self.assertNotEqual(ready[0]["session_id"], ready[1]["session_id"])
                        first = await asyncio.gather(
                            self._exchange(browser, browser_in, browser_out, golden, 0),
                            self._exchange(headless, headless_in, headless_out, golden, 0),
                        )
                        self.assertEqual(first[0], first[1])
                        await asyncio.gather(
                            self._expect_text(
                                browser_in, browser_out,
                                {"type": "ping"},
                                wire_error("unsupported_message_type", "type"),
                            ),
                            self._expect_text(
                                headless_in, headless_out,
                                {"protocol": "avip/1", "type": "playback.stop"},
                                wire_error("unsupported_message_type", "type"),
                            ),
                        )
                        self.assertEqual(app.state.active_sessions, 2)
                        second = await asyncio.gather(
                            self._exchange(browser, browser_in, browser_out, nominal, duration_us(golden)),
                            self._exchange(headless, headless_in, headless_out, nominal, duration_us(golden)),
                        )
                        self.assertEqual(second[0], second[1])
                        self.assertEqual(len(nominal), 640)
                self.assertEqual(app.state.active_sessions, 0)
                self.assertEqual(media.active_mock_sinks(), frozenset())

    async def test_device_label_does_not_select_a_path(self):
        golden = bytes.fromhex(MEDIA["vectors"][0]["payload_hex"])
        with self._audio_available():
            async with app.router.lifespan_context(app):
                swapped = both_clients()
                swapped[0].message["device_id"] = "headless-sim-001"
                swapped[1].message["device_id"] = "browser-demo-001"
                async with connection() as (left_in, left_out):
                    async with connection() as (right_in, right_out):
                        await asyncio.gather(
                            self._handshake(swapped[0], left_in, left_out),
                            self._handshake(swapped[1], right_in, right_out),
                        )
                        echoes = await asyncio.gather(
                            self._exchange(swapped[0], left_in, left_out, golden, 0),
                            self._exchange(swapped[1], right_in, right_out, golden, 0),
                        )
                        self.assertEqual(echoes[0], echoes[1])
                shared_name = both_clients()
                shared_name[1].message["device_id"] = shared_name[0].message["device_id"]
                async with connection() as (left_in, left_out):
                    async with connection() as (right_in, right_out):
                        ready = await asyncio.gather(
                            self._handshake(shared_name[0], left_in, left_out),
                            self._handshake(shared_name[1], right_in, right_out),
                        )
                        self.assertNotEqual(ready[0]["session_id"], ready[1]["session_id"])
                        await asyncio.gather(
                            self._exchange(shared_name[0], left_in, left_out, golden, 0),
                            self._exchange(shared_name[1], right_in, right_out, golden, 0),
                        )

    async def test_default_gateway_refuses_binary_for_both_clients(self):
        payload = TOKEN.encode()
        async with app.router.lifespan_context(app):
            for client in both_clients():
                with self.subTest(client=client.kind):
                    async with connection() as (incoming, outgoing):
                        ready = await self._handshake(client, incoming, outgoing, DISABLED)
                        raw = bytearray(client.pack_uplink(0, 0, payload))
                        await send_frame(incoming, data=bytes(raw))
                        error = await self._json(outgoing)
                        self.assertEqual(error, wire_error("invalid_message", None))
                        self.assertNotIn(TOKEN, json.dumps(error))
                        self.assertNotIn(ready["session_id"], json.dumps(error))
                        await self._close(outgoing, 1003, "Unsupported data")
            self.assertEqual(app.state.active_sessions, 0)

    async def test_unnegotiated_output_queues_without_a_mirror(self):
        available = avip.Capabilities(audio_input=True, audio_output=False)
        expected = dict(DISABLED, audio_input=True)
        payload = b"\x10\x20"
        with patch("avi.main.AVIP_CAPABILITIES", available):
            async with app.router.lifespan_context(app):
                for client in both_clients():
                    with self.subTest(client=client.kind):
                        async with connection() as (incoming, outgoing):
                            ready = await self._handshake(client, incoming, outgoing, expected)
                            self.assertFalse(ready["capabilities"]["audio_output"])
                            await send_frame(incoming, data=client.pack_uplink(0, 0, payload))
                            with self.assertRaises(TimeoutError):
                                async with asyncio.timeout(0.05):
                                    await outgoing.get()
                            self.assertEqual(self._only_sink().consumed, 1)
                            self.assertEqual(self._only_sink().pending, 0)
                            await self._expect_text(
                                incoming, outgoing,
                                {"type": "ping"},
                                wire_error("unsupported_message_type", "type"),
                            )

    async def test_queue_full_stays_open_and_keeps_the_sequence(self):
        payload = b"\x01\x02"
        with self._audio_available():
            async with app.router.lifespan_context(app):
                for client in both_clients():
                    with self.subTest(client=client.kind):
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            # The mock sink drains uplink frames. Pause it so
                            # this test can still fill the depth cap.
                            self._only_sink().pause()
                            clock = 0
                            for sequence in range(media.MAX_QUEUE_DEPTH):
                                raw = client.pack_uplink(sequence, sequence * 20_000, payload)
                                await send_frame(incoming, data=raw)
                                echo = await self._bytes(outgoing)
                                self.assertEqual(echo, mirror(sequence, clock, payload))
                                decoded = client.unpack(echo)
                                self.assertEqual(direction_of(decoded), DOWNLINK)
                                self.assertEqual(sequence_of(decoded), sequence)
                                self.assertEqual(timestamp_of(decoded), clock)
                                self.assertEqual(payload_of(decoded), payload)
                                clock += duration_us(payload)
                            refused = client.pack_uplink(media.MAX_QUEUE_DEPTH, 200_000, payload)
                            await send_frame(incoming, data=refused)
                            error = await self._json(outgoing)
                            self.assertEqual(error, wire_error("queue_full", None))
                            self.assertNotIn(TOKEN, json.dumps(error))
                            self.assertEqual(self._only_sink().consumed, 0)
                            self.assertEqual(self._only_sink().pending, media.MAX_QUEUE_DEPTH)
                            await self._expect_text(
                                incoming, outgoing,
                                {"type": "ping", "request_id": TOKEN},
                                wire_error("unsupported_message_type", "type"),
                            )
                            self.assertNotIn(TOKEN, json.dumps(
                                await self._reject_frame(incoming, outgoing, refused, "queue_full", None)
                            ))
                            ahead = client.pack_uplink(
                                media.MAX_QUEUE_DEPTH + 1, 220_000, payload
                            )
                            await send_frame(incoming, data=ahead)
                            anomaly = await self._json(outgoing)
                            self.assertEqual(anomaly, wire_error("sequence_anomaly", "sequence"))
                            await self._close(outgoing, 1002, "Protocol error")

    async def test_media_protocol_errors_use_stable_envelopes(self):
        payload = TOKEN.encode()
        with self._audio_available():
            async with app.router.lifespan_context(app):
                for client in both_clients():
                    with self.subTest(client=client.kind, case="magic"):
                        raw = bytearray(client.pack_uplink(0, 0, payload))
                        raw[0:4] = b"NOPE"
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            error = await self._reject_frame(
                                incoming, outgoing, bytes(raw),
                                "unsupported_protocol", "magic",
                            )
                            self.assertNotIn(TOKEN, json.dumps(error))
                            self.assertNotIn("NOPE", json.dumps(error))
                            await self._close(outgoing, 1002, "Protocol error")
                    with self.subTest(client=client.kind, case="short"):
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            error = await self._reject_frame(
                                incoming, outgoing, b"NO" + payload,
                                "invalid_frame", "header",
                            )
                            self.assertNotIn(TOKEN, json.dumps(error))
                            await self._close(outgoing, 1002, "Protocol error")
                    with self.subTest(client=client.kind, case="downlink"):
                        downlink = pack_frame(MediaFrame(
                            Direction.AUDIO_DOWNLINK, 0, 0, payload
                        ))
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            error = await self._reject_frame(
                                incoming, outgoing, downlink, "invalid_frame", "direction",
                            )
                            self.assertNotIn(TOKEN, json.dumps(error))
                            await self._close(outgoing, 1002, "Protocol error")
                    with self.subTest(client=client.kind, case="gap"):
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            await self._reject_frame(
                                incoming, outgoing,
                                client.pack_uplink(1, 0, b"\x00\x00"),
                                "sequence_anomaly", "sequence",
                            )
                            await self._close(outgoing, 1002, "Protocol error")
                    with self.subTest(client=client.kind, case="timestamp"):
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            await send_frame(
                                incoming, data=client.pack_uplink(0, 100, b"\x00\x00")
                            )
                            self.assertEqual(
                                await self._bytes(outgoing),
                                mirror(0, 0, b"\x00\x00"),
                            )
                            await self._reject_frame(
                                incoming, outgoing,
                                client.pack_uplink(1, 50, b"\x00\x00"),
                                "timestamp_regression", "timestamp_us",
                            )
                            await self._close(outgoing, 1002, "Protocol error")
                    with self.subTest(client=client.kind, case="too_large"):
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            blob = b"\x00" * (media.MAX_FRAME_BYTES + 1)
                            error = await self._reject_frame(
                                incoming, outgoing, blob, "frame_too_large", "payload",
                            )
                            self.assertNotIn(str(len(blob)), json.dumps(error))
                            await self._close(outgoing, 1002, "Protocol error")
                self.assertEqual(app.state.active_sessions, 0)

    async def test_control_only_client_does_not_open_a_media_path(self):
        message = json.loads(json.dumps(HANDSHAKE["valid"][2]["message"]))
        client = BrowserShapedClient(message)
        client.negotiated = dict(DISABLED)
        with self._audio_available():
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    await self._handshake(client, incoming, outgoing, DISABLED)
                    self.assertEqual(media.active_mock_sinks(), frozenset())
                    await self._reject_frame(
                        incoming, outgoing,
                        client.pack_uplink(0, 0, b"\x00\x00"),
                        "invalid_message", None,
                    )
                    await self._close(outgoing, 1003, "Unsupported data")

    async def test_stalled_mirror_send_closes(self):
        entered = asyncio.Event()
        client = HeadlessPythonClient()

        async def stall(event):
            if event["type"] == "websocket.send" and "bytes" in event:
                entered.set()
                await asyncio.Event().wait()

        with self._audio_available():
            with patch("avi.main.SESSION_LIMITS", SessionLimits(send_timeout=0.05)):
                async with app.router.lifespan_context(app):
                    async with connection(send_hook=stall) as (incoming, outgoing):
                        await self._handshake(client, incoming, outgoing)
                        await send_frame(
                            incoming,
                            data=client.pack_uplink(0, 0, b"\x00\x00"),
                        )
                        await asyncio.wait_for(entered.wait(), 1)
                        await self._close(outgoing, 1013, "Send timeout")
                    self.assertEqual(app.state.active_sessions, 0)

    async def test_mock_sink_sustains_thirty_seconds_of_uplink(self):
        """1500 nominal frames is 30.0 s of 16 kHz pcm_s16le.

        Each frame is 20 ms and 640 bytes. The test does not sleep 20 ms:
        it submits the next frame as soon as the mirror echo returns, faster
        than the media clock. A stuck depth of 8 would reject a frame.
        """
        self.assertEqual(SUSTAINED_FRAMES, 1500)
        self.assertEqual(SUSTAINED_FRAMES * media.NOMINAL_CHUNK_US, SUSTAINED_MEDIA_US)
        self.assertEqual(media.NOMINAL_PAYLOAD_BYTES, 640)
        payload = bytes(media.NOMINAL_PAYLOAD_BYTES)
        with self._audio_available():
            async with app.router.lifespan_context(app):
                for client in both_clients():
                    with self.subTest(client=client.kind):
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            sink = self._only_sink()
                            clock = 0
                            for sequence in range(SUSTAINED_FRAMES):
                                raw = client.pack_uplink(
                                    sequence, sequence * media.NOMINAL_CHUNK_US, payload
                                )
                                await send_frame(incoming, data=raw)
                                echo = await self._bytes(outgoing)
                                self.assertEqual(echo, mirror(sequence, clock, payload))
                                clock += duration_us(payload)
                                self.assertLess(sink.pending, media.MAX_QUEUE_DEPTH)
                            self.assertEqual(clock, SUSTAINED_MEDIA_US)
                            self.assertEqual(sink.consumed, SUSTAINED_FRAMES)
                            self.assertEqual(sink.pending, 0)
                            self.assertEqual(sink.last_sequence, SUSTAINED_FRAMES - 1)
                            self.assertEqual(
                                sink.consumed_bytes, SUSTAINED_FRAMES * len(payload)
                            )
                        self.assertEqual(media.active_mock_sinks(), frozenset())
                        self.assertEqual(self._sink_tasks(), [])

    async def test_mock_sink_sustains_thirty_seconds_of_wall_clock(self):
        """Pace one frame every 20 ms until 30 seconds of wall clock have elapsed.

        The headless client uses the same ``/ws/avip`` path as the browser-shaped
        client. The accelerated test above covers both. This one records how
        many frames were accepted or rejected, the queue depth the test
        observed, the sink high-water mark, and that cleanup removed the task.
        """
        period = media.NOMINAL_CHUNK_US / 1_000_000
        self.assertEqual(period, 0.02)
        payload = bytes(media.NOMINAL_PAYLOAD_BYTES)
        client = HeadlessPythonClient()
        accepted = 0
        rejected = 0
        max_pending = 0
        with self._audio_available():
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    await self._handshake(client, incoming, outgoing)
                    sink = self._only_sink()
                    start = time.monotonic()
                    clock = 0
                    for sequence in range(SUSTAINED_FRAMES):
                        delay = start + sequence * period - time.monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                        await send_frame(
                            incoming,
                            data=client.pack_uplink(
                                sequence, sequence * media.NOMINAL_CHUNK_US, payload
                            ),
                        )
                        event = await asyncio.wait_for(outgoing.get(), 1)
                        if "bytes" in event:
                            accepted += 1
                            self.assertEqual(event["bytes"], mirror(sequence, clock, payload))
                            clock += duration_us(payload)
                        else:
                            rejected += 1
                        max_pending = max(max_pending, sink.pending)
                    remainder = start + 30.0 - time.monotonic()
                    if remainder > 0:
                        await asyncio.sleep(remainder)
                    elapsed = time.monotonic() - start
                    summary = (
                        f"accepted={accepted} rejected={rejected} "
                        f"max_pending={max_pending} high_water={sink.high_water} "
                        f"consumed={sink.consumed} elapsed={elapsed:.3f}"
                    )
                    self.assertGreaterEqual(elapsed, 30.0, summary)
                    self.assertEqual(accepted, SUSTAINED_FRAMES, summary)
                    self.assertEqual(rejected, 0, summary)
                    self.assertEqual(sink.consumed, SUSTAINED_FRAMES, summary)
                    self.assertEqual(sink.pending, 0, summary)
                    self.assertLess(max_pending, media.MAX_QUEUE_DEPTH, summary)
                    self.assertGreaterEqual(sink.high_water, 1, summary)
                    self.assertLess(sink.high_water, media.MAX_QUEUE_DEPTH, summary)
                self.assertEqual(media.active_mock_sinks(), frozenset())
                self.assertEqual(self._sink_tasks(), [])

    async def test_pipelined_burst_does_not_stick_at_the_cap(self):
        client = HeadlessPythonClient()
        payload = b"\x01\x02"
        count = media.MAX_QUEUE_DEPTH + 4
        with self._audio_available():
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    await self._handshake(client, incoming, outgoing)
                    for sequence in range(count):
                        await send_frame(
                            incoming,
                            data=client.pack_uplink(sequence, sequence * 20_000, payload),
                        )
                    for sequence in range(count):
                        echo = await self._bytes(outgoing)
                        decoded = client.unpack(echo)
                        self.assertEqual(sequence_of(decoded), sequence)
                        self.assertEqual(payload_of(decoded), payload)
                    sink = self._only_sink()
                    self.assertEqual(sink.consumed, count)
                    self.assertEqual(sink.pending, 0)
                    self.assertGreaterEqual(sink.high_water, 1)
                    self.assertLess(sink.high_water, media.MAX_QUEUE_DEPTH)

    async def test_paused_sink_rejects_a_pipelined_burst(self):
        client = HeadlessPythonClient()
        payload = b"\x01\x02"
        count = media.MAX_QUEUE_DEPTH + 1
        with self._audio_available():
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    await self._handshake(client, incoming, outgoing)
                    sink = self._only_sink()
                    sink.pause()
                    for sequence in range(count):
                        await send_frame(
                            incoming,
                            data=client.pack_uplink(sequence, sequence * 20_000, payload),
                        )
                    echoes = 0
                    rejected = 0
                    for _ in range(count):
                        event = await asyncio.wait_for(outgoing.get(), 1)
                        if "bytes" in event:
                            echoes += 1
                        else:
                            rejected += 1
                            self.assertEqual(
                                json.loads(event["text"]),
                                wire_error("queue_full", None),
                            )
                    self.assertEqual(echoes, media.MAX_QUEUE_DEPTH)
                    self.assertEqual(rejected, 1)
                    self.assertEqual(sink.consumed, 0)
                    self.assertEqual(sink.pending, media.MAX_QUEUE_DEPTH)
                    self.assertEqual(sink.high_water, media.MAX_QUEUE_DEPTH)

    async def test_paused_and_metered_sink_keep_reject_newest_backpressure(self):
        payload = b"\x01\x02"
        with self._audio_available():
            async with app.router.lifespan_context(app):
                for client in both_clients():
                    with self.subTest(client=client.kind):
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            sink = self._only_sink()
                            sink.pause()
                            for sequence in range(media.MAX_QUEUE_DEPTH):
                                await send_frame(
                                    incoming,
                                    data=client.pack_uplink(
                                        sequence, sequence * 20_000, payload
                                    ),
                                )
                                await self._bytes(outgoing)
                            refused = media.MAX_QUEUE_DEPTH
                            raw = client.pack_uplink(refused, refused * 20_000, payload)
                            await send_frame(incoming, data=raw)
                            self.assertEqual(
                                await self._json(outgoing), wire_error("queue_full", None)
                            )
                            self.assertEqual(sink.consumed, 0)
                            sink.allow(1)
                            self.assertEqual(sink.consumed, 1)
                            self.assertEqual(sink.pending, media.MAX_QUEUE_DEPTH - 1)
                            await send_frame(incoming, data=raw)
                            echo = await self._bytes(outgoing)
                            self.assertEqual(sequence_of(client.unpack(echo)), refused)
                            self.assertEqual(sink.pending, media.MAX_QUEUE_DEPTH)
                            ahead = client.pack_uplink(
                                refused + 1, (refused + 1) * 20_000, payload
                            )
                            await send_frame(incoming, data=ahead)
                            self.assertEqual(
                                await self._json(outgoing), wire_error("queue_full", None)
                            )
                            sink.resume()
                            self.assertEqual(sink.pending, 0)
                            self.assertEqual(sink.consumed, media.MAX_QUEUE_DEPTH + 1)
                            await send_frame(incoming, data=ahead)
                            echo = await self._bytes(outgoing)
                            self.assertEqual(sequence_of(client.unpack(echo)), refused + 1)
                            self.assertEqual(sink.consumed, media.MAX_QUEUE_DEPTH + 2)
                        self.assertEqual(media.active_mock_sinks(), frozenset())
                        self.assertEqual(self._sink_tasks(), [])

    async def test_session_end_cancels_the_sink_and_clears_the_queue(self):
        payload = b"\x11\x22"
        with self._audio_available():
            async with app.router.lifespan_context(app):
                for client in both_clients():
                    with self.subTest(client=client.kind):
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            sink = self._only_sink()
                            sink.pause()
                            await send_frame(
                                incoming, data=client.pack_uplink(0, 0, payload)
                            )
                            await self._bytes(outgoing)
                            self.assertEqual(sink.pending, 1)
                            self.assertEqual(sink.consumed, 0)
                            self.assertTrue(sink.running)
                            self.assertEqual(len(self._sink_tasks()), 1)
                        self.assertFalse(sink.running)
                        self.assertEqual(sink.pending, 0)
                        self.assertEqual(media.active_mock_sinks(), frozenset())
                        self.assertEqual(self._sink_tasks(), [])
                        async with connection() as (incoming, outgoing):
                            await self._handshake(client, incoming, outgoing)
                            await send_frame(
                                incoming, data=client.pack_uplink(0, 0, payload)
                            )
                            self.assertEqual(
                                await self._bytes(outgoing), mirror(0, 0, payload)
                            )
                            self.assertEqual(self._only_sink().consumed, 1)

    def _only_sink(self):
        sinks = media.active_mock_sinks()
        self.assertEqual(len(sinks), 1)
        return next(iter(sinks))

    def _sink_tasks(self):
        return [
            task for task in asyncio.all_tasks()
            if task.get_name() == media.MockMediaSink.TASK_NAME
        ]

    def _audio_available(self):
        return patch(
            "avi.main.AVIP_CAPABILITIES",
            avip.Capabilities(**HANDSHAKE["available_capabilities"]),
        )

    async def _handshake(self, client, incoming, outgoing, expected=None):
        event = await asyncio.wait_for(outgoing.get(), 1)
        self.assertEqual(event["type"], "websocket.accept")
        await send(incoming, client.message)
        ready = await self._json(outgoing)
        self.assertEqual(ready["protocol"], "avip/1")
        self.assertEqual(ready["type"], "session.ready")
        self.assertEqual(ready["request_id"], client.message["request_id"])
        self.assertEqual(
            ready["capabilities"],
            client.negotiated if expected is None else expected,
        )
        self.assertRegex(ready["session_id"], r"^s[0-9a-f]{32}$")
        self.assertNotIn("device_id", ready)
        self.assertNotEqual(ready["session_id"], client.message["device_id"])
        return ready

    async def _exchange(self, client, incoming, outgoing, payload, timestamp_us):
        sequence = client.sequence
        await send_frame(incoming, data=client.next_uplink(payload))
        echo = await self._bytes(outgoing)
        self.assertEqual(echo, mirror(sequence, timestamp_us, payload))
        decoded = client.unpack(echo)
        self.assertEqual(payload_of(decoded), payload)
        self.assertEqual(direction_of(decoded), DOWNLINK)
        self.assertEqual(sequence_of(decoded), sequence)
        self.assertEqual(timestamp_of(decoded), timestamp_us)
        return echo

    async def _expect_text(self, incoming, outgoing, message, expected):
        await send(incoming, message)
        self.assertEqual(await self._json(outgoing), expected)

    async def _reject_frame(self, incoming, outgoing, blob, code, field):
        await send_frame(incoming, data=blob)
        error = await self._json(outgoing)
        self.assertEqual(error, wire_error(code, field))
        return error

    async def _json(self, outgoing):
        event = await asyncio.wait_for(outgoing.get(), 1)
        self.assertIn("text", event)
        self.assertNotIn("bytes", event)
        return json.loads(event["text"])

    async def _bytes(self, outgoing):
        event = await asyncio.wait_for(outgoing.get(), 1)
        self.assertEqual(event["type"], "websocket.send")
        self.assertIn("bytes", event)
        self.assertNotIn("text", event)
        return event["bytes"]

    async def _close(self, outgoing, code, reason):
        event = await asyncio.wait_for(outgoing.get(), 1)
        self.assertEqual(event, {"type": "websocket.close", "code": code, "reason": reason})
