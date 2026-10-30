from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DeviceCapabilities:
    microphone: bool = False
    speaker: bool = False
    motion: bool = False
    expressions: bool = False


class DeviceAdapter(Protocol):
    capabilities: DeviceCapabilities

    async def stop_playback(self) -> None: ...
    async def play_audio(self, pcm: bytes, sample_rate: int) -> None: ...
