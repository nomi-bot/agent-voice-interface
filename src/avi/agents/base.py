from dataclasses import dataclass
from typing import Literal, Protocol

RunStatus = Literal["running", "completed", "cancelled", "failed"]


@dataclass(frozen=True)
class AgentCapabilities:
    status: bool = False
    cancellation: bool = False


@dataclass(frozen=True)
class Run:
    id: str
    status: RunStatus
    result: str | None = None


class AgentAdapter(Protocol):
    capabilities: AgentCapabilities

    async def submit(self, request: str) -> Run: ...
    async def status(self, run_id: str) -> Run: ...
    async def cancel(self, run_id: str) -> Run: ...
    async def close(self) -> None: ...
