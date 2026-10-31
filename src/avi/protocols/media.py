"""AVIP v1 binary media frames and bounded delivery.

This module is transport-independent. It does not open sockets, attach the
session handshake, route audio to a provider, or conceal loss. A later
WebSocket adapter can pass one complete binary message to ``unpack_frame``
and submit the result here without defining a second layout.
"""

from collections import deque
from dataclasses import dataclass
from enum import IntEnum
import struct

from avi.protocols.avip import ProtocolError


MAGIC = b"AVIP"
VERSION = 1
ENDIANNESS = "little"
HEADER_FORMAT = "<4sBBBBIQI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
_HEADER = struct.Struct(HEADER_FORMAT)

MAGIC_OFFSET = 0
VERSION_OFFSET = 4
DIRECTION_OFFSET = 5
CODEC_OFFSET = 6
RESERVED_OFFSET = 7
SEQUENCE_OFFSET = 8
TIMESTAMP_OFFSET = 12
PAYLOAD_LENGTH_OFFSET = 20

# M0 mock profile shared by browser-shaped and headless clients. This is not a
# measured provider format. Sample-rate negotiation and any other codec are M1.
SAMPLE_RATE_HZ = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
NOMINAL_CHUNK_US = 20_000
NOMINAL_PAYLOAD_BYTES = (
    SAMPLE_RATE_HZ * CHANNELS * SAMPLE_WIDTH_BYTES * NOMINAL_CHUNK_US // 1_000_000
)
MAX_PAYLOAD_BYTES = 8192
MAX_FRAME_BYTES = HEADER_SIZE + MAX_PAYLOAD_BYTES
# Eight nominal 20 ms frames is 160 ms of queued audio per direction.
MAX_QUEUE_DEPTH = 8
_SEQUENCE_MODULUS = 0x1_0000_0000
_UINT32_MAX = _SEQUENCE_MODULUS - 1
_UINT64_MAX = 0xFFFF_FFFF_FFFF_FFFF
_DEPTH_ERROR = f"max_depth must be an integer from 1 to {MAX_QUEUE_DEPTH}"


class Direction(IntEnum):
    """Audio direction relative to the client, matching capability names."""

    AUDIO_UPLINK = 1
    AUDIO_DOWNLINK = 2


class Codec(IntEnum):
    """Codec identifier carried in every frame. M0 accepts only PCM s16le."""

    PCM_S16LE = 1


_DIRECTIONS = {member.value: member for member in Direction}
_CODECS = {member.value: member for member in Codec}


def next_sequence(sequence: int) -> int:
    """Return the only legal successor of a sequence number, modulo 2**32."""
    if type(sequence) is not int or not 0 <= sequence <= _UINT32_MAX:
        raise ProtocolError("invalid_frame", "sequence")
    return (sequence + 1) % _SEQUENCE_MODULUS


def _require_direction(value: object) -> Direction:
    if type(value) is Direction:
        return value
    if type(value) is int and value in _DIRECTIONS:
        return _DIRECTIONS[value]
    raise ProtocolError("invalid_frame", "direction")


def _require_codec(value: object) -> Codec:
    if type(value) is Codec:
        codec = value
    elif type(value) is int and value in _CODECS:
        codec = _CODECS[value]
    elif type(value) is int:
        raise ProtocolError("unsupported_codec", "codec")
    else:
        raise ProtocolError("invalid_frame", "codec")
    if codec is not Codec.PCM_S16LE:
        raise ProtocolError("unsupported_codec", "codec")
    return codec


def _require_uint(value: object, upper: int, field: str) -> int:
    if type(value) is not int or not 0 <= value <= upper:
        raise ProtocolError("invalid_frame", field)
    return value


def _require_payload(value: object) -> bytes:
    if type(value) not in (bytes, bytearray, memoryview):
        raise ProtocolError("invalid_frame", "payload")
    if type(value) is memoryview and (
        value.ndim != 1 or value.itemsize != 1 or value.format not in {"B", "b"}
    ):
        raise ProtocolError("invalid_frame", "payload")
    length = len(value)
    if length > MAX_PAYLOAD_BYTES:
        raise ProtocolError("frame_too_large", "payload")
    if length < SAMPLE_WIDTH_BYTES or length % SAMPLE_WIDTH_BYTES != 0:
        raise ProtocolError("invalid_frame", "payload")
    return bytes(value)


@dataclass(frozen=True)
class MediaFrame:
    """One validated audio frame. Payload bytes are an immutable copy."""

    direction: Direction
    sequence: int
    timestamp_us: int
    payload: bytes
    codec: Codec = Codec.PCM_S16LE

    def __post_init__(self) -> None:
        direction = _require_direction(self.direction)
        codec = _require_codec(self.codec)
        sequence = _require_uint(self.sequence, _UINT32_MAX, "sequence")
        timestamp_us = _require_uint(self.timestamp_us, _UINT64_MAX, "timestamp_us")
        payload = _require_payload(self.payload)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "codec", codec)
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(self, "timestamp_us", timestamp_us)
        object.__setattr__(self, "payload", payload)


def pack_frame(frame: MediaFrame) -> bytes:
    """Encode one frame. Multi-byte integers are little-endian."""
    if type(frame) is not MediaFrame:
        raise ProtocolError("invalid_frame")
    return _HEADER.pack(
        MAGIC,
        VERSION,
        frame.direction,
        frame.codec,
        0,
        frame.sequence,
        frame.timestamp_us,
        len(frame.payload),
    ) + frame.payload


def _as_frame_bytes(value: object) -> bytes:
    if type(value) is memoryview:
        if value.ndim != 1 or value.itemsize != 1 or value.format not in {"B", "b"}:
            raise ProtocolError("invalid_frame")
    elif type(value) is not bytes and type(value) is not bytearray:
        raise ProtocolError("invalid_frame")
    if len(value) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large", "payload")
    if len(value) < HEADER_SIZE:
        raise ProtocolError("invalid_frame", "header")
    return bytes(value)


def unpack_frame(buffer: object) -> MediaFrame:
    """Decode one complete frame. Partial frames are not buffered.

    The caller supplies one whole frame. Oversized input is rejected before
    the header is interpreted. Error text never includes buffer contents.
    """
    data = _as_frame_bytes(buffer)
    magic, version, direction, codec, reserved, sequence, timestamp_us, payload_length = (
        _HEADER.unpack_from(data)
    )
    if magic != MAGIC:
        raise ProtocolError("unsupported_protocol", "magic")
    if version != VERSION:
        raise ProtocolError("unsupported_protocol", "version")
    if reserved != 0:
        raise ProtocolError("invalid_frame", "reserved")
    if direction not in _DIRECTIONS:
        raise ProtocolError("invalid_frame", "direction")
    if codec not in _CODECS:
        raise ProtocolError("unsupported_codec", "codec")
    if payload_length > MAX_PAYLOAD_BYTES:
        raise ProtocolError("frame_too_large", "payload")
    payload = data[HEADER_SIZE:]
    if payload_length != len(payload):
        raise ProtocolError("invalid_frame", "payload")
    return MediaFrame(
        direction=_DIRECTIONS[direction],
        sequence=sequence,
        timestamp_us=timestamp_us,
        payload=payload,
        codec=_CODECS[codec],
    )


class MediaStream:
    """Per-direction sequence and timestamp continuity for one session.

    The first frame in each direction must use sequence 0. Every later frame
    must use ``next_sequence`` of the last accepted frame. Timestamps are a
    sender media clock in microseconds and may only stay the same or move
    forward. They are not wall-clock times and must not be subtracted across
    hosts or directions. A failed check leaves the stream unchanged.
    """

    def __init__(self) -> None:
        self._expected = {
            Direction.AUDIO_UPLINK: 0,
            Direction.AUDIO_DOWNLINK: 0,
        }
        self._last_timestamp: dict[Direction, int | None] = {
            Direction.AUDIO_UPLINK: None,
            Direction.AUDIO_DOWNLINK: None,
        }

    def check(self, frame: MediaFrame) -> None:
        if type(frame) is not MediaFrame:
            raise ProtocolError("invalid_frame")
        if frame.sequence != self._expected[frame.direction]:
            raise ProtocolError("sequence_anomaly", "sequence")
        last = self._last_timestamp[frame.direction]
        if last is not None and frame.timestamp_us < last:
            raise ProtocolError("timestamp_regression", "timestamp_us")

    def commit(self, frame: MediaFrame) -> None:
        self.check(frame)
        self._expected[frame.direction] = next_sequence(frame.sequence)
        self._last_timestamp[frame.direction] = frame.timestamp_us

    def accept(self, frame: MediaFrame) -> None:
        self.commit(frame)


class MediaQueue:
    """FIFO of already validated frames with a hard depth cap.

    When the queue is full, ``offer`` raises ``queue_full`` and leaves the
    queued frames in place. Nothing is dropped to make room. ``clear`` is a
    separate, explicit flush. The queue does not await, reorder, or close a
    connection, and it is not safe to share across threads.
    """

    def __init__(self, max_depth: int = MAX_QUEUE_DEPTH):
        if type(max_depth) is not int or not 1 <= max_depth <= MAX_QUEUE_DEPTH:
            raise ValueError(_DEPTH_ERROR)
        self.max_depth = max_depth
        self._items: deque[MediaFrame] = deque()

    def offer(self, frame: MediaFrame) -> None:
        if type(frame) is not MediaFrame:
            raise ProtocolError("invalid_frame")
        if len(self._items) >= self.max_depth:
            raise ProtocolError("queue_full")
        self._items.append(frame)

    def take(self) -> MediaFrame | None:
        if not self._items:
            return None
        return self._items.popleft()

    def clear(self) -> int:
        removed = len(self._items)
        self._items.clear()
        return removed

    def __len__(self) -> int:
        return len(self._items)


class MediaDelivery:
    """Ordered delivery with an independent bound for each direction.

    Sequence state advances only after the frame is queued. A full queue
    therefore does not consume the sequence: the sender retries that same
    frame. Uplink and downlink do not share depth or sequence state.
    """

    def __init__(self, max_depth: int = MAX_QUEUE_DEPTH):
        self._stream = MediaStream()
        self._queues = {
            Direction.AUDIO_UPLINK: MediaQueue(max_depth),
            Direction.AUDIO_DOWNLINK: MediaQueue(max_depth),
        }

    def submit(self, frame: MediaFrame) -> None:
        if type(frame) is not MediaFrame:
            raise ProtocolError("invalid_frame")
        self._stream.check(frame)
        self._queues[frame.direction].offer(frame)
        self._stream.commit(frame)

    def ingest(self, buffer: object) -> MediaFrame:
        frame = unpack_frame(buffer)
        self.submit(frame)
        return frame

    def take(self, direction: object) -> MediaFrame | None:
        return self._queues[_require_direction(direction)].take()

    def depth(self, direction: object) -> int:
        return len(self._queues[_require_direction(direction)])

    def clear(self, direction: object | None = None) -> int:
        if direction is None:
            return sum(queue.clear() for queue in self._queues.values())
        return self._queues[_require_direction(direction)].clear()
