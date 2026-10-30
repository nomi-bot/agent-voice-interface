import asyncio
from uuid import uuid4

from avi.agents.base import AgentCapabilities, Run


class MockAgentAdapter:
    """Session-owned, bounded mock. Never executes commands or external tools."""

    capabilities = AgentCapabilities(status=True, cancellation=True)

    def __init__(self, delay: float = 1.0, max_runs: int = 32):
        self.delay = delay
        self.max_runs = max_runs
        self._runs: dict[str, Run] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def submit(self, request: str) -> Run:
        if not request.strip() or len(request) > 4096:
            raise ValueError("Request must contain 1–4096 characters")
        if len(self._runs) >= self.max_runs:
            raise ValueError("Session run limit reached")
        run = Run(uuid4().hex, "running")
        self._runs[run.id] = run
        self._tasks[run.id] = asyncio.create_task(self._execute(run.id, request))
        return run

    async def _execute(self, run_id: str, request: str) -> None:
        await asyncio.sleep(self.delay)
        self._runs[run_id] = Run(run_id, "completed", f"Mock completed: {request}")

    async def status(self, run_id: str) -> Run:
        return self._runs[run_id]

    async def cancel(self, run_id: str) -> Run:
        run = self._runs[run_id]
        if run.status == "running":
            self._tasks[run_id].cancel()
            await asyncio.gather(self._tasks[run_id], return_exceptions=True)
            self._runs[run_id] = Run(run_id, "cancelled")
        return self._runs[run_id]

    async def close(self) -> None:
        for run_id in self._runs:
            await self.cancel(run_id)
