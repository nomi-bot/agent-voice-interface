from typing import Protocol


class TransportDisconnected(Exception):
    """The peer has disconnected or the connection can no longer send."""


class TransportAdapter(Protocol):
    async def receive(self) -> dict: ...
    async def send(self, event: dict) -> None: ...
    async def close(self, code: int, reason: str) -> None: ...
