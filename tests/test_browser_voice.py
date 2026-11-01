"""Browser microphone and speaker checks that do not need a real browser."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

from avi.main import app


ROOT = Path(__file__).parents[1]
BROWSER = ROOT / "clients" / "browser"
VOICE_CHECK = BROWSER / "voice-check.mjs"


class BrowserVoiceChecks(unittest.TestCase):
    def test_node_voice_checks(self):
        completed = subprocess.run(
            ["node", str(VOICE_CHECK)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        report = json.loads(completed.stdout)
        self.assertTrue(report["ok"])
        names = [item["name"] for item in report["checks"]]
        self.assertIn("stop-playback-is-local", names)
        self.assertIn("permission-denied-opens-no-socket", names)
        self.assertIn("track-ended-stops-capture-and-leaves-the-session-open", names)
        self.assertIn("avip-disconnect-releases-the-microphone-and-speaker", names)
        self.assertTrue(all(item["ok"] for item in report["checks"]))

    def test_voice_client_has_no_agent_runtime(self):
        voice = (BROWSER / "voice.js").read_text()
        page = (BROWSER / "voice.html").read_text()
        self.assertNotIn("hermes", voice.lower())
        self.assertNotIn("hermes", page.lower())
        self.assertIn("robot_actions: false", page)
        self.assertIn("stopPlayback", voice)
        self.assertNotIn("playback.stop", voice)


class VoicePageTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_serves_only_the_voice_demo_files(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
        ) as client:
            health = await client.get("/health")
            self.assertEqual(health.json()["voice"], "not_implemented")
            page = await client.get("/voice")
            self.assertEqual(page.status_code, 200)
            self.assertIn("text/html", page.headers["content-type"])
            self.assertEqual(page.headers["x-content-type-options"], "nosniff")
            self.assertIn("Microphone", page.text)
            self.assertIn("AVIP_MOCK_MEDIA=1", page.text)
            self.assertIn("Session clock", page.text)
            self.assertIn("telemetry-report", page.text)
            self.assertIn("/voice/voice.js", page.text)
            self.assertIn("/voice/avip-client.js", page.text)
            self.assertIn(b"/voice/capture-worklet.js", (BROWSER / "voice.js").read_bytes())
            for name in ("voice.js", "pcm.js", "avip-client.js", "capture-worklet.js", "telemetry.js"):
                asset = await client.get(f"/voice/{name}")
                self.assertEqual(asset.status_code, 200, name)
                self.assertEqual(asset.content, (BROWSER / name).read_bytes(), name)
            self.assertEqual((await client.get("/voice/voice.html")).status_code, 404)
            self.assertEqual((await client.get("/voice/nope.js")).status_code, 404)
            self.assertEqual((await client.get("/voice/%2e%2e%2fvoice.js")).status_code, 404)

    async def test_synthetic_mic_round_trip_against_mock_media(self):
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
        try:
            await _wait_health(port, process, log)
            completed = await asyncio.to_thread(
                subprocess.run,
                ["node", str(VOICE_CHECK), "live", f"ws://127.0.0.1:{port}/ws/avip"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr + _log_text(log))
            report = json.loads(completed.stdout)
            self.assertTrue(report["ok"])
            self.assertEqual(report["uplink"], 2)
            self.assertEqual(report["downlink"], 2)
            self.assertEqual(report["played"], 1)
            telemetry = report["report"]
            self.assertEqual(telemetry["schema"], "avi.voice-telemetry/1")
            self.assertEqual(telemetry["path"], "mock-mirror")
            self.assertEqual(telemetry["client"], "browser")
            self.assertEqual(telemetry["counters"]["uplinkFrames"], 2)
            self.assertEqual(telemetry["counters"]["downlinkFrames"], 2)
            self.assertEqual(telemetry["counters"]["speakerDrops"], 0)
            self.assertEqual(telemetry["e2e"]["clock"], "session")
            self.assertEqual(telemetry["e2e"]["uplinkToDownlink"]["count"], 2)
            self.assertEqual(telemetry["e2e"]["uplinkToAudible"]["count"], 1)
            self.assertIsNone(telemetry["cost"]["provider"])
            self.assertIsNone(telemetry["cost"]["amount"])
            self.assertIn("session-start", telemetry["marks"])
            self.assertIn("playback-start", telemetry["marks"])
            self.assertNotIn("AIza", json.dumps(telemetry))
        finally:
            await asyncio.to_thread(_stop_process, process)
            log.close()


def _stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _log_text(log):
    log.seek(0)
    return log.read()


async def _wait_health(port, process, log):
    deadline = time.monotonic() + 10
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(_log_text(log))
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(_log_text(log) or "gateway did not become healthy")
