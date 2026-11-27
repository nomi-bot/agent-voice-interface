"""Headless stream workload with independent microphone and scripted socket output."""
import json
import unittest

from websockets.asyncio.server import serve

from avip_client import AUDIO_DOWNLINK, AUDIO_UPLINK, AvipError, pack_frame, unpack_frame
from avip_client.simulator import mock_pcm, run_simulation


class StreamingSimulatorTests(unittest.IsolatedAsyncioTestCase):
    async def simulate(self, exchange):
        async def gateway(socket):
            start = json.loads(await socket.recv())
            await socket.send(json.dumps({
                "protocol": "avip/1", "type": "session.ready", "session_id": "sim",
                "request_id": start["request_id"], "capabilities": start["capabilities"],
            }))
            await exchange(socket)
            await socket.wait_closed()

        async with serve(gateway, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            return await run_simulation(f"ws://127.0.0.1:{port}/ws/avip", frames=3, scenario="stream")

    async def test_independent_audio_after_uplink_and_interruption(self):
        uplinks = []

        async def exchange(socket):
            for _ in range(3):
                uplinks.append(unpack_frame(await socket.recv()))
            await socket.send(pack_frame(AUDIO_DOWNLINK, 0, 0, mock_pcm(12)))
            interrupted = json.dumps({"protocol": "avip/1", "type": "response.interrupted", "generation": 0})
            await socket.send(interrupted)
            await socket.send(interrupted)
            await socket.send(pack_frame(AUDIO_DOWNLINK, 1, 20_000, mock_pcm(13)))

        report = await self.simulate(exchange)
        self.assertEqual([frame.payload for frame in uplinks], [mock_pcm(i) for i in range(3)])
        self.assertEqual((report.uplink_frames, report.downlink_frames, report.interruptions), (3, 2, 1))
        self.assertGreaterEqual(report.played_frames, 1)
        self.assertEqual(report.played_frames + report.flushed_frames, 2)

    async def test_silent_provider_finishes_with_observed_counts(self):
        async def silent(socket):
            await socket.wait_closed()

        report = await self.simulate(silent)
        self.assertEqual((report.uplink_frames, report.downlink_frames, report.played_frames), (3, 0, 0))

    async def test_wrong_downlink_direction_fails_the_run(self):
        async def invalid(socket):
            await socket.recv()
            await socket.send(pack_frame(AUDIO_UPLINK, 0, 0, mock_pcm(0)))

        with self.assertRaises(AvipError) as error:
            await self.simulate(invalid)
        self.assertEqual(error.exception.code, "invalid_downlink")
