"""Observable simulator cases against the browser CLI and the real mock gateway."""

import asyncio
import json
import subprocess
import sys
import tempfile
import unittest

from avip_client import AUDIO_DOWNLINK, AudioFrame, pack_frame
from avip_client.simulator import Speaker, mock_pcm, run_simulation
from test_avip_sdk_conformance import (
    CLI,
    ROOT,
    _free_port,
    _mock_media_env,
    _node,
    _stop_process,
    _wait_health,
)


def interrupt(generation):
    return {"protocol": "avip/1", "type": "response.interrupted", "generation": generation}


class SimulatorSpeakerTests(unittest.TestCase):
    def test_only_the_active_generation_is_flushed(self):
        first = AudioFrame(AUDIO_DOWNLINK, 0, 0, mock_pcm(0))
        second = AudioFrame(AUDIO_DOWNLINK, 1, 20_000, mock_pcm(1))
        speaker = Speaker()
        speaker.enqueue(first)
        speaker.interrupt(interrupt(0))
        speaker.enqueue(second)
        speaker.interrupt(interrupt(0))
        self.assertEqual(speaker.flushed, 1)
        self.assertEqual(speaker.play_next(), second)

    def test_stop_flushes_audio_and_stays_local(self):
        frame = AudioFrame(AUDIO_DOWNLINK, 0, 0, mock_pcm(0))
        speaker = Speaker()
        speaker.enqueue(frame)
        speaker.stop()
        speaker.interrupt(interrupt(0))
        self.assertFalse(speaker.enqueue(frame))
        self.assertIsNone(speaker.play_next())
        self.assertEqual((speaker.flushed, speaker.dropped), (1, 1))


class SimulatorGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        port = _free_port()
        self.url = f"ws://127.0.0.1:{port}/ws/avip"
        log = self.enterContext(tempfile.TemporaryFile(mode="w+"))
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "avi.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--ws-max-size", "16384", "--ws-max-queue", "16"],
            env=_mock_media_env(), stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
        )
        self.addAsyncCleanup(asyncio.to_thread, _stop_process, process)
        await _wait_health(port, process, log)

    async def test_browser_cli_and_simulator_share_the_mock_gateway(self):
        browser = json.loads(await asyncio.to_thread(
            _node, [str(CLI), "roundtrip", self.url, mock_pcm(0).hex()],
        ))
        simulator = await run_simulation(self.url, frames=3)
        self.assertEqual(simulator.capabilities, browser["capabilities"])
        self.assertFalse(simulator.capabilities["robot_actions"])
        self.assertEqual(browser["downlink_hex"], pack_frame(
            AUDIO_DOWNLINK, 0, 0, mock_pcm(0),
        ).hex())
        self.assertEqual((simulator.uplink_frames, simulator.played_frames), (3, 3))

    async def test_reconnect_restarts_framing_and_completes_workload(self):
        report = await run_simulation(self.url, frames=6, scenario="disconnect")
        self.assertEqual((report.sessions, report.uplink_frames, report.played_frames), (2, 6, 6))

    async def test_slow_speaker_has_bounded_queue_with_accounted_drops(self):
        report = await run_simulation(self.url, frames=24, scenario="slow-speaker")
        self.assertEqual(report.downlink_frames, 24)
        self.assertEqual(report.peak_queue_frames, 8)
        self.assertGreater(report.dropped_frames, 0)
        self.assertEqual(report.played_frames + report.dropped_frames, 24)

    async def test_cli_stop_keeps_the_mock_mic_running(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "avip_client.simulator", self.url,
            "--scenario", "stop-playback", "--frames", "6",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=ROOT,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        self.assertEqual(process.returncode, 0, stderr.decode())
        report = json.loads(stdout)
        self.assertEqual((report["uplink_frames"], report["downlink_frames"]), (6, 6))
        self.assertEqual((report["played_frames"], report["dropped_frames"]), (3, 3))


if __name__ == "__main__":
    unittest.main()
