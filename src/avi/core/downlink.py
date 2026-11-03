"""Pace provider audio onto AVIP downlink.

The browser speaker drops a frame once 160 ms is already scheduled. Provider
chunks can be 256 ms, and a faster-than-real-time burst would overflow that
horizon even if it were split. This pacer emits nominal 20 ms frames and
keeps the amount sent less than 160 ms ahead of a monotonic clock. Eight
unsent frames is the queue bound. An interruption drops only the unsent
frames of the current generation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
import time

from avi.core.events import VoiceEvent
from avi.protocols.avip import response_interrupted
from avi.protocols.media import NOMINAL_CHUNK_US, NOMINAL_PAYLOAD_BYTES, SAMPLE_RATE_HZ, SAMPLE_WIDTH_BYTES


AHEAD_US = 8 * NOMINAL_CHUNK_US
_QUEUE_FRAMES = 8


def split_pcm(payload: bytes) -> list[bytes]:
    """Split PCM into nominal 20 ms frames. The tail may be shorter."""

    if not payload:
        return []
    step = NOMINAL_PAYLOAD_BYTES
    return [payload[offset:offset + step] for offset in range(0, len(payload), step)]


def frame_duration_us(payload: bytes) -> int:
    samples = len(payload) // SAMPLE_WIDTH_BYTES
    return samples * 1_000_000 // SAMPLE_RATE_HZ


def pace_wait_us(
    sent_us: int, elapsed_us: int, *, ahead_us: int = AHEAD_US, quantum_us: int = NOMINAL_CHUNK_US,
) -> int:
    """Return how long to wait so the next frame still fits under ``ahead_us``.

    A wait of one quantum is the minimum once the horizon is already full.
    One microsecond would admit a frame and immediately fill the speaker again.
    """

    if sent_us - elapsed_us < ahead_us:
        return 0
    deficit = sent_us - elapsed_us - ahead_us + 1
    return max(deficit, quantum_us)


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
        self.sent_us = 0
        self._origin: float | None = None
        self._space = asyncio.Event()
        self._space.set()
        self._sender: asyncio.Task[None] | None = None
        self._sender_error: BaseException | None = None
        self._closed = False

    def _elapsed_us(self) -> int:
        if self._origin is None:
            return 0
        return max(0, int((self._monotonic() - self._origin) * 1_000_000))

    def _note_queued(self) -> None:
        self.high_water = max(self.high_water, self.queue.qsize())

    def _drop_queued(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._space.set()

    def enqueue_available(self, payload: bytes, generation: int) -> bytes:
        """Queue as many frames as fit. Return the unsent tail.

        A generation change drops the tail. The caller keeps it only while
        ``generation`` is still current.
        """

        if generation != self.generation or self._closed:
            return b""
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            if generation != self.generation:
                return b""
            chunk = bytes(view[offset:offset + NOMINAL_PAYLOAD_BYTES])
            try:
                self.queue.put_nowait((generation, chunk))
            except asyncio.QueueFull:
                self._space.clear()
                if generation != self.generation:
                    return b""
                try:
                    self.queue.put_nowait((generation, chunk))
                except asyncio.QueueFull:
                    return bytes(view[offset:])
            self._note_queued()
            offset += len(chunk)
        return b""

    async def interrupt(self) -> int:
        """Drop unsent audio for this generation and tell the client."""

        flushed = self.generation
        self.generation += 1
        self._drop_queued()
        self._origin = None
        self.sent_us = 0
        self._space.set()
        await self._send_json(response_interrupted(flushed))
        return flushed

    async def _wait_room(self, generation: int, frame_us: int) -> bool:
        while generation == self.generation and not self._closed:
            delay = pace_wait_us(self.sent_us, self._elapsed_us())
            if delay <= 0:
                return True
            await self._sleeper(min(delay, NOMINAL_CHUNK_US) / 1_000_000)
        return False

    async def _send_loop(self) -> None:
        try:
            while not self._closed:
                item = await self.queue.get()
                self._space.set()
                if item is None or self._closed:
                    return
                generation, chunk = item
                if generation != self.generation:
                    continue
                duration = frame_duration_us(chunk)
                if not await self._wait_room(generation, duration):
                    continue
                if self._origin is None:
                    self._origin = self._monotonic()
                ahead = self.sent_us - self._elapsed_us()
                if ahead < 0:
                    ahead = 0
                self.max_ahead_us = max(self.max_ahead_us, ahead)
                await self._send_frame(chunk)
                self.sent_us += duration
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
        pending = b""
        pending_gen = self.generation
        try:
            while True:
                self._raise_sender()
                if self._sender is not None and self._sender.done():
                    await self._sender
                    return
                if pending and pending_gen == self.generation:
                    pending = self.enqueue_available(pending, pending_gen)
                if pending and pending_gen == self.generation:
                    await self._wait_for_space_or_event(fetch)
                    if fetch is not None and fetch.done():
                        event = fetch.result()
                        fetch = asyncio.create_task(self._next_event(iterator))
                        if event is None:
                            return
                        pending, pending_gen = await self._on_event(
                            event, pending, pending_gen, on_control
                        )
                    continue
                if fetch is None:
                    return
                await self._wait_for_event(fetch)
                event = fetch.result()
                fetch = asyncio.create_task(self._next_event(iterator))
                if event is None:
                    return
                pending, pending_gen = await self._on_event(
                    event, pending, pending_gen, on_control
                )
        finally:
            if fetch is not None and not fetch.done():
                fetch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await fetch
            await asyncio.shield(self.aclose())

    async def _wait_for_space_or_event(self, fetch: asyncio.Task[VoiceEvent | None]) -> None:
        space = asyncio.create_task(self._space.wait())
        watched = {space, fetch}
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
        pending: bytes,
        pending_gen: int,
        on_control: Callable[[VoiceEvent], Awaitable[None]],
    ) -> tuple[bytes, int]:
        if event.kind == "audio":
            payload = event.payload
            if type(payload) is not bytes or len(payload) % SAMPLE_WIDTH_BYTES != 0:
                raise ValueError("provider audio")
            if pending_gen != self.generation:
                pending = b""
            return pending + payload, self.generation
        if event.kind == "interrupted":
            await self.interrupt()
            return b"", self.generation
        await on_control(event)
        return pending, pending_gen

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.generation += 1
        self._drop_for_close()
        sender = self._sender
        self._sender = None
        if sender is None:
            return
        if not sender.done():
            sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender

    def _drop_for_close(self) -> None:
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._space.set()
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
