"""Stateful PCM resampler for the AVIP 16 kHz profile.

Live API output is 24 kHz. AVIP playback is 16 kHz. One converter keeps the
fractional read cursor across chunks so a long utterance is one stream, not a
series of independently rounded blocks. This module does not open a network
connection, write audio, or know which provider produced the bytes.
"""

from __future__ import annotations

import math


_INT16_MIN = -32768
_INT16_MAX = 32767


class StreamingResampler:
    """Linear interpolator for mono PCM s16le.

    ``push`` returns every sample whose two source neighbours are already
    buffered. The unread tail stays on the converter until the next push or
    ``reset``. Equal rates are copied through and do not interpolate.
    """

    def __init__(self, from_rate: int, to_rate: int):
        if type(from_rate) is not int or type(to_rate) is not int:
            raise ValueError("sample rates must be positive integers")
        if from_rate <= 0 or to_rate <= 0:
            raise ValueError("sample rates must be positive integers")
        self.from_rate = from_rate
        self.to_rate = to_rate
        self._step = from_rate / to_rate
        self._pending: list[int] = []
        self._pos = 0.0

    def reset(self) -> None:
        """Drop buffered input. The next push does not blend with the old tail."""
        self._pending.clear()
        self._pos = 0.0

    def end_turn(self) -> None:
        """Discard the unread tail at an utterance boundary.

        A short final chunk can sit in ``_pending`` until a later push supplies
        its neighbour. That neighbour must not be the next utterance. The tail
        is dropped here instead of being emitted after ``turn_complete``.
        """

        self.reset()

    def push(self, pcm: bytes) -> bytes:
        if type(pcm) is not bytes or len(pcm) % 2 != 0:
            raise ValueError("PCM payload must be even-length bytes")
        if self.from_rate == self.to_rate:
            return pcm
        if pcm:
            self._pending.extend(_decode_s16le(pcm))
        produced: list[int] = []
        pending = self._pending
        pos = self._pos
        step = self._step
        limit = len(pending) - 1
        while pos + 1 < len(pending):
            index = math.floor(pos)
            if index >= limit:
                break
            fraction = pos - index
            left = pending[index]
            right = pending[index + 1]
            produced.append(_clamp_i16(left + (right - left) * fraction))
            pos += step
        drop = math.floor(pos)
        if drop > 0:
            del pending[:drop]
            pos -= drop
        self._pos = pos
        return _encode_s16le(produced)


def _decode_s16le(pcm: bytes) -> list[int]:
    return [
        int.from_bytes(pcm[index : index + 2], "little", signed=True)
        for index in range(0, len(pcm), 2)
    ]


def _encode_s16le(samples: list[int]) -> bytes:
    return b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples)


def _clamp_i16(value: float) -> int:
    rounded = math.floor(value + 0.5)
    if rounded > _INT16_MAX:
        return _INT16_MAX
    if rounded < _INT16_MIN:
        return _INT16_MIN
    return rounded
