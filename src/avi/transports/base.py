from typing import Protocol


class TransportDisconnected(Exception):
    """The peer has disconnected or the connection can no longer send."""


class TransportFrameError(Exception):
    """A frame that cannot be read as a JSON text object.

    ``binary`` is true when the frame was not text. The payload is discarded
    and is not stored on the exception.
    """

    def __init__(self, *, binary: bool):
        self.binary = binary
        super().__init__("unsupported binary frame" if binary else "invalid text frame")


class TransportAdapter(Protocol):
    async def receive(self) -> dict: ...
    async def send(self, event: dict) -> None: ...
    async def close(self, code: int, reason: str) -> None: ...
