from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class VoiceEvent:
    kind: Literal["audio", "interrupted", "tool_call", "turn_complete", "error"]
    payload: bytes | dict[str, Any] | None = None
