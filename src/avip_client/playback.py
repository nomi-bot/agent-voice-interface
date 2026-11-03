"""Playback generation for a headless speaker.

``response.interrupted`` flushes audio queued for that generation. Audio
enqueued afterward is the next generation and is kept. This does not open a
socket or call a provider.
"""

from __future__ import annotations


PROTOCOL = "avip/1"
INTERRUPTED = "response.interrupted"


class PlaybackBuffer:
    """Bounded queue of one active playback generation."""

    def __init__(self, max_frames: int = 8):
        if type(max_frames) is not int or max_frames < 1:
            raise ValueError("max_frames")
        self.max_frames = max_frames
        self.generation = 0
        self.queued: list[bytes] = []
        self.accepted = 0
        self.dropped = 0
        self.flushed = 0

    def enqueue(self, payload: bytes) -> bool:
        if type(payload) is not bytes or len(payload) < 2 or len(payload) % 2 != 0:
            raise ValueError("payload")
        if len(self.queued) >= self.max_frames:
            self.dropped += 1
            return False
        self.queued.append(payload)
        self.accepted += 1
        return True

    def interrupt(self, generation: int) -> bool:
        """Drop queued audio when ``generation`` is the active one."""

        if type(generation) is not int or generation != self.generation:
            return False
        self.flushed += len(self.queued)
        self.queued.clear()
        self.generation += 1
        return True

    def apply(self, message: object) -> bool:
        """Apply one server text object. Unrelated objects are ignored."""

        if not isinstance(message, dict):
            return False
        if message.get("protocol") != PROTOCOL or message.get("type") != INTERRUPTED:
            return False
        generation = message.get("generation")
        if type(generation) is not int:
            return False
        return self.interrupt(generation)
