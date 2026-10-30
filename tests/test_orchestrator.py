import asyncio
import unittest

from avi.agents.mock import MockAgentAdapter
from avi.core.orchestrator import VoiceOrchestrator


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_task_does_not_block_control_and_stopping_speech(self):
        agent = MockAgentAdapter(delay=60)
        control = VoiceOrchestrator(agent)
        try:
            async with asyncio.timeout(0.5):
                run = await control.handle({"type": "submit", "request": "long task"})
                self.assertEqual(await control.handle({"type": "ping"}), {"type": "pong"})
                await control.handle({"type": "stop_playback"})
                self.assertEqual((await agent.status(run["id"])).status, "running")
                self.assertEqual((await control.handle({"type": "cancel_response"}))["type"], "unsupported")
                cancelled = await control.handle({"type": "cancel_task", "run_id": run["id"]})
                self.assertEqual(cancelled["status"], "cancelled")
        finally:
            await agent.close()
