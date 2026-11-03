"""Pace provider audio onto AVIP downlink.

The browser speaker drops a frame once 160 ms is already scheduled. This
pacer emits nominal 20 ms frames and keeps the playout deadline less than
160 ms ahead of the monotonic clock. After a gap the deadline is behind
the clock, so the next answer starts from now instead of spending the
silence as send credit.

Pacer-owned audio (pending bytes, the eight-frame queue, and one in-flight
frame) stays within ``MAX_BACKLOG_BYTES``. Further audio events while that
budget is full are dropped so an interruption or other control event can
still be read. The current event is copied in slices as room appears, so a
paced multi-second answer is not truncated.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
import time

from avi.core.events import VoiceEvent
from avi.protocols.avip import response_interrupted
from avi.protocols.media import NOMINAL_CHUNK_US, NOMINAL_PAYLOAD_BYTES, SAMPLE_WIDTH_BYTES


AHEAD_US = 8 * NOMINAL_CHUNK_US
_QUEUE_FRAMES = 8
# Eight queued frames plus the one frame the sender has removed from the queue.
MAX_BACKLOG_BYTES = (_QUEUE_FRAMES + 1) * NOMINAL_PAYLOAD_BYTES


def frame_duration_us(payload: bytes) -> int:
    samples = len(payload) // SAMPLE_WIDTH_BYTES
    return samples * 1_000_000 // 16_000


def pace_wait_us(
    deadline_us: int, now_us: int, *, ahead_us: int = AHEAD_US, quantum_us: int = NOMINAL_CHUNK_US,
) -> int:
    """Wait so ``deadline_us`` stays under ``ahead_us`` ahead of ``now_us``.

    The caller rebases a deadline that is already behind ``now_us``. A full
    horizon waits at least one 20 ms quantum so the speaker gains a slot.
    """

    lead = deadline_us - now_us
    if lead < ahead_us:
        return 0
    return max(lead - ahead_us + 1, quantum_us)


class DownlinkPacer:
    """Bounded playout from provider audio events to AVIP frames."""

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
        self.queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue(maxsize=_QUEUE_FRAMES)
        self.generation = 0
        self.high_water = 0
        self.max_ahead_us = 0
        self.max_backlog_bytes = 0
        self.overflow_bytes = 0
        self._pending = bytearray()
        self._queued_bytes = 0
        self._inflight_bytes = 0
        self._deadline_us: int | None = None
        self._space = asyncio.Event()
        self._space.set()
        self._sender: asyncio.Task[None] | None = None
        self._sender_error: BaseException | None = None
        self._closed = False

    def backlog_bytes(self) -> int:
        """Bytes this pacer is holding, not audio still inside the provider."""

        return len(self._pending) + self._queued_bytes + self._inflight_bytes

    def _now_us(self) -> int:
        return int(self._monotonic() * 1_000_000)

    def _rebase(self) -> int:
        """Pull a deadline that underran back to the current clock."""

        now = self._now_us()
        if self._deadline_us is None or self._deadline_us < now:
            self._deadline_us = now
        return now

    def _note_backlog(self) -> None:
        self.max_backlog_bytes = max(self.max_backlog_bytes, self.backlog_bytes())

    def _note_queued(self) -> None:
        self.high_water = max(self.high_water, self.queue.qsize())

    def _flush_pending(self, *, drain: bool = False) -> None:
        while len(self._pending) >= 2:
            if not drain and len(self._pending) < NOMINAL_PAYLOAD_BYTES:
                return
            step = min(NOMINAL_PAYLOAD_BYTES, len(self._pending))
            step -= step % 2
            if step < 2:
                return
            chunk = bytes(self._pending[:step])
            try:
                self.queue.put_nowait((self.generation, chunk))
            except asyncio.QueueFull:
                self._space.clear()
                return
            del self._pending[:step]
            self._queued_bytes += len(chunk)
            self._note_queued()
            self._note_backlog()

    def _discard_audio(self) -> None:
        self._pending.clear()
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queued_bytes = 0
        self._note_backlog()
        self._space.set()

    async def interrupt(self) -> int:
        """Drop unsent audio for this generation and tell the client."""

        flushed = self.generation
        self.generation += 1
        self._discard_audio()
        self._deadline_us = None
        await self._send_json(response_interrupted(flushed))
        return flushed

    async def _wait_room(self, generation: int) -> bool:
        while generation == self.generation and not self._closed:
            now = self._rebase()
            delay = pace_wait_us(self._deadline_us or now, now)
            if delay <= 0:
                return True
            await self._sleeper(min(delay, NOMINAL_CHUNK_US) / 1_000_000)
        return False

    async def _send_loop(self) -> None:
        try:
            while not self._closed:
                item = await self.queue.get()
                if item is None or self._closed:
                    return
                generation, chunk = item
                self._queued_bytes -= len(chunk)
                self._inflight_bytes += len(chunk)
                self._space.set()
                self._note_backlog()
                try:
                    if generation != self.generation:
                        continue
                    duration = frame_duration_us(chunk)
                    if not await self._wait_room(generation):
                        continue
                    now = self._rebase()
                    lead = (self._deadline_us or now) - now
                    if lead < 0:
                        lead = 0
                    self.max_ahead_us = max(self.max_ahead_us, lead)
                    await self._send_frame(chunk)
                    if self._deadline_us is None:
                        self._deadline_us = self._now_us()
                    self._deadline_us += duration
                finally:
                    self._inflight_bytes = max(0, self._inflight_bytes - len(chunk))
                    self._note_backlog()
                    self._space.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._sender_error = exc
            self._space.set()
            raise

    def _raise_sender(self) -> None:
        if self._sender_error is not None:
            raise self._sender_error

    async def _next_event(self, iterator) -> VoiceEvent | None:
        try:
            return await anext(iterator)
        except StopAsyncIteration:
            return None

    def _copy_room(self, payload: memoryview, offset: int) -> int:
        """Copy what fits into pending. Return the new offset."""

        room = MAX_BACKLOG_BYTES - self.backlog_bytes()
        if room < 2 or offset >= len(payload):
            return offset
        take = min(room, len(payload) - offset)
        take -= take % 2
        if take < 2:
            return offset
        self._pending.extend(payload[offset:offset + take])
        self._note_backlog()
        return offset + take

    async def run(
        self,
        events: AsyncIterator[VoiceEvent],
        on_control: Callable[[VoiceEvent], Awaitable[None]],
    ) -> None:
        """Read provider events, pace audio, and surface control events."""

        self._sender = asyncio.create_task(self._send_loop(), name="avip-downlink-pace")
        iterator = events.__aiter__()
        fetch: asyncio.Task[VoiceEvent | None] | None = asyncio.create_task(
            self._next_event(iterator)
        )
        audio: memoryview | None = None
        offset = 0
        audio_gen = self.generation
        try:
            while True:
                self._raise_sender()
                if self._sender is not None and self._sender.done():
                    await self._sender
                    return
                if audio is not None and audio_gen != self.generation:
                    audio = None
                if audio is not None:
                    offset = self._copy_room(audio, offset)
                    self._flush_pending(drain=offset >= len(audio))
                    if offset < len(audio):
                        self._space.clear()
                        if self.backlog_bytes() < MAX_BACKLOG_BYTES:
                            continue
                        await self._wait_for_space_or_event(fetch)
                        if fetch is not None and fetch.done():
                            event = fetch.result()
                            if event is None:
                                fetch = None
                                continue
                            fetch = asyncio.create_task(self._next_event(iterator))
                            audio, offset, audio_gen = await self._on_event(
                                event, audio, offset, audio_gen, on_control
                            )
                        continue
                    audio = None
                if fetch is None:
                    self._flush_pending(drain=True)
                    return
                await self._wait_for_event(fetch)
                if fetch is None or not fetch.done():
                    continue
                event = fetch.result()
                if event is None:
                    self._flush_pending(drain=True)
                    return
                fetch = asyncio.create_task(self._next_event(iterator))
                audio, offset, audio_gen = await self._on_event(
                    event, audio, offset, audio_gen, on_control
                )
        finally:
            if fetch is not None and not fetch.done():
                fetch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await fetch
            await asyncio.shield(self.aclose())

    async def _wait_for_space_or_event(self, fetch: asyncio.Task[VoiceEvent | None] | None) -> None:
        space = asyncio.create_task(self._space.wait())
        watched = {space}
        if fetch is not None:
            watched.add(fetch)
        if self._sender is not None:
            watched.add(self._sender)
        try:
            done, _pending = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not space.done():
                space.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await space
        if self._sender is not None and self._sender in done:
            self._raise_sender()

    async def _wait_for_event(self, fetch: asyncio.Task[VoiceEvent | None]) -> None:
        watched = {fetch}
        if self._sender is not None:
            watched.add(self._sender)
        await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        if self._sender is not None and self._sender.done() and not fetch.done():
            self._raise_sender()
            await self._sender

    async def _on_event(
        self,
        event: VoiceEvent,
        audio: memoryview | None,
        offset: int,
        audio_gen: int,
        on_control: Callable[[VoiceEvent], Awaitable[None]],
    ) -> tuple[memoryview | None, int, int]:
        if event.kind == "audio":
            payload = event.payload
            if type(payload) is not bytes or len(payload) % 2 != 0:
                raise ValueError("provider audio")
            if audio is not None and offset < len(audio):
                # The current answer is already using the backlog budget.
                self.overflow_bytes += len(payload)
                return audio, offset, audio_gen
            if self.backlog_bytes() >= MAX_BACKLOG_BYTES:
                self.overflow_bytes += len(payload)
                return None, 0, self.generation
            return memoryview(payload), 0, self.generation
        if event.kind == "interrupted":
            await self.interrupt()
            return None, 0, self.generation
        await on_control(event)
        return audio, offset, audio_gen

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.generation += 1
        self._discard_audio()
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        sender = self._sender
        self._sender = None
        if sender is None:
            return
        if not sender.done():
            sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender
