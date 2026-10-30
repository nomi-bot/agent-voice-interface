import asyncio
import unittest

from avi.agents.mock import MockAgentAdapter


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion(self):
        agent = MockAgentAdapter(delay=0)
        run = await agent.submit("hello")
        await asyncio.sleep(0.01)
        self.assertEqual((await agent.status(run.id)).result, "Mock completed: hello")
        await agent.close()

    async def test_cancel_and_cleanup(self):
        agent = MockAgentAdapter(delay=60)
        run = await agent.submit("long task")
        await agent.close()
        self.assertEqual((await agent.status(run.id)).status, "cancelled")
        self.assertTrue(all(task.done() for task in agent._tasks.values()))

    async def test_validation_and_limit(self):
        agent = MockAgentAdapter(delay=60, max_runs=1)
        with self.assertRaises(ValueError):
            await agent.submit(" ")
        await agent.submit("first")
        with self.assertRaises(ValueError):
            await agent.submit("second")
        await agent.close()
