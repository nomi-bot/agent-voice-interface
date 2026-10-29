from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from avi.core.events import VoiceEvent


@dataclass(frozen=True)
class VoiceCapabilities:
    full_duplex: bool = False
    tool_calls: bool = False
    response_cancellation: bool = False


class VoiceProvider(Protocol):
    capabilities: VoiceCapabilities

    async def start(self) -> None: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    def events(self) -> AsyncIterator[VoiceEvent]: ...
    async def tool_result(self, call_id: str, result: dict) -> None: ...
    async def cancel_response(self) -> None: ...
    async def close(self) -> None: ...
