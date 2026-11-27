from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class TargetedResponseCancellation(Protocol):
    """Providers label audible events with a session-local response ID."""

    async def cancel_response_if_current(self, response_id: int) -> None: ...


@dataclass(frozen=True)
class TokenUsage:
    provider: str
    input_tokens: int | None
    output_tokens: int | None


@runtime_checkable
class TokenUsageReporter(Protocol):
    """A provider with one latest usage snapshot scoped to this connection."""

    def latest_token_usage(self) -> TokenUsage | None: ...
