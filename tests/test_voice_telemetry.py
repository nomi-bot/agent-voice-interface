"""Voice telemetry reports, the mock soak, and gateway counters.

The live probe starts a loopback gateway with ``AVIP_MOCK_MEDIA=1``. It does
not read provider credentials. The in-process soak does not open a socket.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from avi.core.avip_session import AvipSession
from avi.core.session import SessionLimits
from avi.protocols.avip import Capabilities
from avi.protocols.media import Direction, MediaFrame, pack_frame
from avi.telemetry import (
    MAX_UPLINK_QUEUED_BYTES,
    SCHEMA,
    UPLINK_CLOSE_QUEUED_BYTES,
    UPLINK_FRAME_BYTES,
    VoiceTelemetry,
    frames_for_seconds,
    in_process_soak,
    probe_mock_mirror,
    uplink_queue_decision,
)
from avi.transports.base import TransportDisconnected
from test_browser_voice import _free_port, _stop_process, _wait_health


ROOT = Path(__file__).parents[1]
FIXTURES = json.loads((ROOT / "tests" / "fixtures" / "avip-v1-handshake.json").read_text())
START = FIXTURES["valid"][0]["message"]
SOAK = ROOT / "clients" / "browser" / "voice-soak.mjs"
SECRET = "AIzaSyTESTSECRETVALUE"


def _norm(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _norm(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_norm(item) for item in value]
    return value


class Peer:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []
        self.closed = None

    async def receive_message(self):
        if not self.messages:
            raise TransportDisconnected
        return self.messages.pop(0)

    async def send(self, event):
        self.sent.append(event)

    async def send_bytes(self, payload):
        self.sent.append(payload)

    async def close(self, code, reason):
        self.closed = (code, reason)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class AdvanceOnMirror(Peer):
    def __init__(self, messages, clock):
        super().__init__(messages)
        self.clock = clock

    async def send_bytes(self, payload):
        self.clock.t += 5
        self.sent.append(payload)


class PauseBeforeMedia(Peer):
    def __init__(self, messages, session):
        super().__init__(messages)
        self.session = session
        self.paused = False

    async def receive_message(self):
        sink = self.session.sink
        if sink is not None and not self.paused:
            sink.pause()
            self.paused = True
        return await super().receive_message()


class IdleAfterReady(Peer):
    async def receive_message(self):
        if self.messages:
            return self.messages.pop(0)
        await asyncio.Event().wait()


class StallMirror(Peer):
    async def send_bytes(self, payload):
        self.sent.append(payload)
        await asyncio.Event().wait()


def _uplink(sequence, payload):
    return pack_frame(MediaFrame(Direction.AUDIO_UPLINK, sequence, sequence * 20_000, payload))


class VoiceTelemetryTests(unittest.TestCase):
    def test_distribution_percentiles_and_cost_refusal(self):
        telemetry = VoiceTelemetry(
            client="headless",
            scenario="page",
            now=lambda: 0.0,
            wall_now=lambda: 1.0,
        )
        for value in (10, 20, 30, 40):
            telemetry.e2e.record(value)
        snapshot = telemetry.e2e.snapshot()
        self.assertEqual(snapshot["count"], 4)
        self.assertEqual(snapshot["min"], 10)
        self.assertEqual(snapshot["max"], 40)
        self.assertEqual(snapshot["p50"], 20)
        self.assertEqual(snapshot["p95"], 40)
        self.assertFalse(snapshot["approximate"])
        self.assertEqual(sum(item["count"] for item in snapshot["histogram"]), 4)
        telemetry.note_cost({"provider": "gemini", "apiKey": SECRET, "amount": 1})
        telemetry.note_cost({"provider": SECRET, "amount": 1})
        telemetry.note_timeout(SECRET)
        report = telemetry.report()
        self.assertIsNone(report["cost"]["provider"])
        self.assertIsNone(report["cost"]["amount"])
        self.assertEqual(report["timeouts"], [{"kind": "other", "sessionMs": 0.0}])
        self.assertNotIn(SECRET, json.dumps(report))
        telemetry.note_cost({"provider": "gemini", "inputTokens": 3, "amount": 0.5})
        accepted = telemetry.report()
        self.assertEqual(accepted["cost"]["provider"], "gemini")
        self.assertEqual(accepted["cost"]["inputTokens"], 3)
        self.assertEqual(accepted["cost"]["amount"], 0.5)
        self.assertIsNone(accepted["cost"]["currency"])
        self.assertEqual(accepted["schema"], SCHEMA)
        self.assertEqual(accepted["clocks"]["session"]["name"], "local")
        self.assertIn("performance.now", accepted["clocks"]["session"]["description"])
        self.assertIn("Do not subtract", accepted["clocks"]["media"]["description"])
        self.assertFalse(accepted["clocks"]["wall"]["comparableAcrossHosts"])

    def test_uplink_budget_matches_the_browser_thresholds(self):
        self.assertEqual(UPLINK_FRAME_BYTES, 664)
        self.assertEqual(uplink_queue_decision(0), "send")
        self.assertEqual(uplink_queue_decision(MAX_UPLINK_QUEUED_BYTES - UPLINK_FRAME_BYTES), "send")
        self.assertEqual(
            uplink_queue_decision(MAX_UPLINK_QUEUED_BYTES - UPLINK_FRAME_BYTES + 1),
            "drop",
        )
        self.assertEqual(uplink_queue_decision(MAX_UPLINK_QUEUED_BYTES), "drop")
        self.assertEqual(uplink_queue_decision(UPLINK_CLOSE_QUEUED_BYTES), "close")
        self.assertEqual(uplink_queue_decision(None), "close")
        self.assertEqual(uplink_queue_decision(-1), "close")

    def test_soak_duration_and_drop_counters(self):
        self.assertEqual(frames_for_seconds(0.4), 20)
        self.assertEqual(frames_for_seconds(2), 100)
        with self.assertRaises(ValueError):
            frames_for_seconds(0)
        with self.assertRaises(ValueError):
            frames_for_seconds(1801)
        report = in_process_soak(20)
        self.assertEqual(report["scenario"], "mock-soak")
        self.assertEqual(report["path"], "mock-mirror")
        self.assertEqual(report["client"], "headless")
        self.assertEqual(report["counters"]["uplinkFrames"], 20)
        self.assertEqual(report["counters"]["playbackFrames"], 8)
        self.assertEqual(report["counters"]["speakerDrops"], 12)
        self.assertEqual(report["counters"]["captureDrops"], 1)
        self.assertEqual(report["counters"]["transmitDrops"], 3)
        self.assertEqual(report["counters"]["timeouts"], 0)
        self.assertIsNone(report["cost"]["provider"])
        self.assertIsNone(report["cost"]["amount"])
        self.assertEqual(report["marks"]["first-uplink"]["sessionMs"], 10)
        self.assertEqual(report["marks"]["playback-start"]["sessionMs"], 15)
        self.assertEqual(report["intervalsMs"]["uplinkToDownlink"], 5)
        self.assertEqual(report["e2e"]["scope"], "mock-mirror")
        self.assertEqual(report["e2e"]["clock"], "session")
        self.assertEqual(report["e2e"]["uplinkToDownlink"]["count"], 20)
        self.assertEqual(report["e2e"]["uplinkToDownlink"]["p95"], 5)
        self.assertEqual(report["e2e"]["uplinkToAudible"]["count"], 8)
        self.assertEqual(report["buffer"]["jitter"]["count"], 19)
        self.assertEqual(report["buffer"]["depthMaxFrames"], 8)
        self.assertEqual(report["clocks"]["wall"]["anchorWallMs"], 1_700_000_000_000)
        self.assertNotIn(SECRET, json.dumps(report))

    def test_python_and_node_soaks_match(self):
        completed = subprocess.run(
            ["node", str(SOAK), "--seconds", "0.4"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        browser = json.loads(completed.stdout)
        headless = in_process_soak(20)
        self.assertEqual(browser["client"], "browser")
        self.assertEqual(headless["client"], "headless")
        browser["client"] = headless["client"] = "soak"
        self.assertEqual(_norm(browser), _norm(headless))
        cli = subprocess.run(
            [sys.executable, "-m", "avi.telemetry", "--seconds", "0.4"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(_norm(json.loads(cli.stdout)), _norm(in_process_soak(20)))
        refused = subprocess.run(
            [sys.executable, "-m", "avi.telemetry", "--seconds", "1801"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertNotIn("GEMINI_API_KEY", refused.stderr)


class GatewayTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_mirror_latency_uses_the_gateway_session_clock(self):
        payload = b"\x5a\xa5" * 320
        clock = Clock()
        transport = AdvanceOnMirror([("text", START), ("binary", _uplink(0, payload))], clock)
        session = AvipSession(
            transport,
            SessionLimits(),
            Capabilities(audio_input=True, audio_output=True),
            clock=clock,
            wall_clock=lambda: 50.0,
        )
        with self.assertLogs("avi.core.avip_session", level="INFO") as captured:
            await session.run()
        report = session.telemetry_report
        self.assertEqual(report["client"], "gateway")
        self.assertEqual(report["scenario"], "gateway")
        self.assertEqual(report["marks"]["session-start"]["sessionMs"], 0)
        self.assertEqual(report["marks"]["first-uplink"]["sessionMs"], 0)
        self.assertEqual(report["marks"]["first-downlink"]["sessionMs"], 5)
        self.assertNotIn("playback-start", report["marks"])
        self.assertEqual(report["intervalsMs"]["uplinkToDownlink"], 5)
        self.assertEqual(report["e2e"]["uplinkToDownlink"]["count"], 1)
        self.assertEqual(report["e2e"]["uplinkToDownlink"]["min"], 5)
        self.assertEqual(report["counters"]["uplinkFrames"], 1)
        self.assertEqual(report["counters"]["downlinkFrames"], 1)
        self.assertEqual(report["buffer"]["sinkHighWater"], 1)
        self.assertEqual(report["buffer"]["sinkConsumed"], 1)
        self.assertRegex(report["sessionId"], r"^s[0-9a-f]{32}$")
        self.assertEqual(report["sessionId"], transport.sent[0]["session_id"])
        text = "\n".join(captured.output)
        self.assertIn("voice_telemetry", text)
        self.assertNotIn(payload.hex(), text)
        self.assertNotIn(SECRET, text)
        self.assertIsNone(report["cost"]["amount"])

    async def test_paused_sink_counts_queue_full_without_closing(self):
        payload = bytes(640)
        session = AvipSession(
            Peer([]),
            SessionLimits(),
            Capabilities(audio_input=True, audio_output=True),
            clock=lambda: 0.0,
            wall_clock=lambda: 0.0,
        )
        messages = [("text", START)]
        messages.extend(("binary", _uplink(sequence, payload)) for sequence in range(9))
        session.transport = PauseBeforeMedia(messages, session)
        await session.run()
        report = session.telemetry_report
        self.assertEqual(report["counters"]["queueFull"], 1)
        self.assertEqual(report["counters"]["uplinkFrames"], 8)
        self.assertEqual(report["counters"]["downlinkFrames"], 8)
        self.assertEqual(report["buffer"]["depthMaxFrames"], 8)
        self.assertEqual(report["buffer"]["sinkHighWater"], 8)
        self.assertIsNone(session.transport.closed)
        errors = [item for item in session.transport.sent if isinstance(item, dict)]
        self.assertTrue(any(item.get("code") == "queue_full" for item in errors))

    async def test_idle_and_send_timeouts_are_tokens(self):
        idle = AvipSession(
            IdleAfterReady([("text", START)]),
            SessionLimits(idle_timeout=0.05),
            Capabilities(audio_input=True, audio_output=True),
        )
        await idle.run()
        self.assertEqual(idle.transport.closed, (1001, "Idle timeout"))
        self.assertEqual(
            [item["kind"] for item in idle.telemetry_report["timeouts"]],
            ["idle"],
        )
        stalled = AvipSession(
            StallMirror([("text", START), ("binary", _uplink(0, bytes(640)))]),
            SessionLimits(send_timeout=0.05),
            Capabilities(audio_input=True, audio_output=True),
        )
        await stalled.run()
        self.assertEqual(stalled.transport.closed, (1013, "Send timeout"))
        self.assertEqual(stalled.telemetry_report["counters"]["uplinkFrames"], 1)
        self.assertNotIn("first-downlink", stalled.telemetry_report["marks"])
        self.assertEqual(
            [item["kind"] for item in stalled.telemetry_report["timeouts"]],
            ["send"],
        )


class LiveMockProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_headless_and_browser_probes_skip_without_secrets(self):
        port = _free_port()
        log = tempfile.TemporaryFile(mode="w+")
        env = os.environ.copy()
        env["AVIP_MOCK_MEDIA"] = "1"
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
            await _wait_health(port, process, log)
            headless = await probe_mock_mirror(url, frames=2)
            self.assertEqual(headless["scenario"], "mock-probe")
            self.assertEqual(headless["client"], "headless")
            self.assertEqual(headless["counters"]["uplinkFrames"], 2)
            self.assertEqual(headless["counters"]["speakerDrops"], 0)
            self.assertEqual(headless["e2e"]["uplinkToDownlink"]["count"], 2)
            self.assertGreaterEqual(headless["e2e"]["uplinkToDownlink"]["min"], 0)
            self.assertNotIn("playback-start", headless["marks"])
            self.assertRegex(headless["sessionId"], r"^s[0-9a-f]{32}$")
            self.assertIsNone(headless["cost"]["amount"])
            completed = await asyncio.to_thread(
                subprocess.run,
                ["node", str(SOAK), "--frames", "2", "--live", url],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            browser = json.loads(completed.stdout)["live"]
            self.assertEqual(browser["client"], "browser")
            self.assertEqual(browser["counters"]["downlinkFrames"], 2)
            self.assertEqual(browser["counters"]["playbackFrames"], 2)
            self.assertEqual(browser["counters"]["speakerDrops"], 0)
            self.assertIn("playback-start", browser["marks"])
            self.assertIsNone(browser["cost"]["provider"])
            self.assertNotIn(SECRET, completed.stdout)
        finally:
            await asyncio.to_thread(_stop_process, process)
            log.close()
