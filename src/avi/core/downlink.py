"""Buffer provider PCM and pace AVIP playback within the speaker horizon."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import time

from avi.core.events import VoiceEvent
from avi.protocols.avip import response_interrupted
from avi.protocols.media import (
    NOMINAL_CHUNK_US,
    NOMINAL_PAYLOAD_BYTES,
    SAMPLE_RATE_HZ,
    SAMPLE_WIDTH_BYTES,
)


AHEAD_US = 8 * NOMINAL_CHUNK_US
# Allow a short model response to arrive in a burst, including the active frame.
MAX_BACKLOG_BYTES = 3 * SAMPLE_RATE_HZ * SAMPLE_WIDTH_BYTES


class DownlinkPacer:
    """Read provider controls while sending audio from a bounded PCM buffer."""

    def __init__(
        self,
        send_frame: Callable[[bytes], Awaitable[None]],
        send_json: Callable[[dict], Awaitable[None]],
        *,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ):
        self._send_frame = send_frame
        self._send_json = send_json
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._sleeper = asyncio.sleep if sleeper is None else sleeper
        self._pending = bytearray()
        self._audio_ready = asyncio.Event()
        self._inflight_bytes = 0
        self._deadline_us = 0
        self.generation = 0
        self.max_ahead_us = 0
        self.max_backlog_bytes = 0
        self.overflow_bytes = 0

    def backlog_bytes(self) -> int:
        return len(self._pending) + self._inflight_bytes

    def _now_us(self) -> int:
        return int(self._monotonic() * 1_000_000)

    def _buffer_audio(self, payload: object) -> None:
        if type(payload) is not bytes or len(payload) % SAMPLE_WIDTH_BYTES:
            raise ValueError("provider audio")
        room = MAX_BACKLOG_BYTES - self.backlog_bytes()
        accepted = min(room, len(payload))
        self.overflow_bytes += len(payload) - accepted
        if accepted:
            self._pending.extend(memoryview(payload)[:accepted])
            self.max_backlog_bytes = max(self.max_backlog_bytes, self.backlog_bytes())
            self._audio_ready.set()

    async def _interrupt(self) -> None:
        flushed = self.generation
        self.generation += 1
        self._pending.clear()
        self._audio_ready.clear()
        self._deadline_us = 0
        await self._send_json(response_interrupted(flushed))

    async def _wait_room(self, generation: int, duration_us: int) -> int | None:
        while generation == self.generation:
            now = self._now_us()
            deadline = max(self._deadline_us, now)
            delay = deadline + duration_us - now - AHEAD_US
            if delay <= 0:
                self.max_ahead_us = max(self.max_ahead_us, deadline + duration_us - now)
                return deadline
            await self._sleeper(delay / 1_000_000)
        return None

    async def _send_loop(self) -> None:
        while True:
            await self._audio_ready.wait()
            generation = self.generation
            chunk = bytes(self._pending[:NOMINAL_PAYLOAD_BYTES])
            del self._pending[:len(chunk)]
            if not self._pending:
                self._audio_ready.clear()
            self._inflight_bytes = len(chunk)
            try:
                samples = len(chunk) // SAMPLE_WIDTH_BYTES
                duration_us = samples * 1_000_000 // SAMPLE_RATE_HZ
                deadline = await self._wait_room(generation, duration_us)
                if deadline is not None:
                    await self._send_frame(chunk)
                    if generation == self.generation:
                        self._deadline_us = max(deadline, self._now_us()) + duration_us
            finally:
                self._inflight_bytes = 0

    async def _read_events(
        self,
        events: AsyncIterator[VoiceEvent],
        on_control: Callable[[VoiceEvent], Awaitable[None]],
    ) -> None:
        async for event in events:
            if event.kind == "audio":
                self._buffer_audio(event.payload)
            elif event.kind == "interrupted":
                await self._interrupt()
            else:
                await on_control(event)

    async def run(
        self,
        events: AsyncIterator[VoiceEvent],
        on_control: Callable[[VoiceEvent], Awaitable[None]],
    ) -> None:
        sender = asyncio.create_task(self._send_loop(), name="avip-downlink-pace")
        reader = asyncio.create_task(self._read_events(events, on_control), name="avip-downlink-events")
        try:
            done, _ = await asyncio.wait((sender, reader), return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                sender.result()
            if reader in done:
                reader.result()
        finally:
            sender.cancel()
            reader.cancel()
            self._pending.clear()
            self._audio_ready.clear()
            await asyncio.gather(sender, reader, return_exceptions=True)
