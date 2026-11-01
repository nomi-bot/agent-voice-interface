"""AVIP v1 binary media frames for the headless client.

This encoder does not import ``avi.protocols``. The gateway has its own
pack and unpack. The browser adapter has a third. Conformance compares the
bytes; none of the three calls the others.
"""

from dataclasses import dataclass

from avip_client.errors import AvipError


MAGIC = b"AVIP"
VERSION = 1
HEADER_SIZE = 24
AUDIO_UPLINK = 1
AUDIO_DOWNLINK = 2
CODEC_PCM_S16LE = 1
SAMPLE_RATE_HZ = 16_000
SAMPLE_WIDTH_BYTES = 2
NOMINAL_CHUNK_US = 20_000
NOMINAL_PAYLOAD_BYTES = (
    SAMPLE_RATE_HZ * SAMPLE_WIDTH_BYTES * NOMINAL_CHUNK_US // 1_000_000
)
MAX_PAYLOAD_BYTES = 8192
MAX_FRAME_BYTES = HEADER_SIZE + MAX_PAYLOAD_BYTES
_DIRECTIONS = frozenset({AUDIO_UPLINK, AUDIO_DOWNLINK})
_UINT32_MAX = 0xFFFF_FFFF
_UINT64_MAX = 0xFFFF_FFFF_FFFF_FFFF


@dataclass(frozen=True)
class AudioFrame:
    """One decoded audio frame. ``payload`` is a copy."""

    direction: int
    sequence: int
    timestamp_us: int
    payload: bytes
    codec: int = CODEC_PCM_S16LE


class UplinkClock:
    """Sender clock for client capture.

    Sequence starts at 0 and wraps at 2**32. ``timestamp_us`` starts at 0.
    ``encode`` does not move the clock. ``commit`` advances the timestamp by
    the nominal 20 ms chunk, which matches a 640-byte payload on the M0 clock.
    It is not the gateway mirror clock.

    Call ``commit`` only after the socket has accepted the frame. Acceptance
    means the bytes were queued locally. It is not a peer acknowledgement.
    If send raises, do not commit: the same sequence may be retried on this
    socket while it is still open. If send returns and the socket later
    closes, the frame may or may not have been delivered. Do not send that
    sequence again on this socket. M0 does not reconnect. A new connection is
    a new session and starts at sequence 0.
    """

    def __init__(self) -> None:
        self.sequence = 0
        self.timestamp_us = 0

    def encode(self, payload: bytes) -> bytes:
        """Return the current uplink frame without moving the clock."""
        return pack_frame(AUDIO_UPLINK, self.sequence, self.timestamp_us, payload)

    def commit(self) -> None:
        """Advance after the socket has accepted the encoded frame."""
        self.sequence = (self.sequence + 1) % 2**32
        self.timestamp_us += NOMINAL_CHUNK_US


def pack_frame(
    direction: int,
    sequence: int,
    timestamp_us: int,
    payload: bytes | bytearray,
    *,
    codec: int = CODEC_PCM_S16LE,
) -> bytes:
    """Encode one complete frame. Multi-byte integers are little-endian."""
    direction = _require_direction(direction)
    codec = _require_codec(codec)
    sequence = _require_uint(sequence, _UINT32_MAX, "sequence")
    timestamp_us = _require_uint(timestamp_us, _UINT64_MAX, "timestamp_us")
    body = _require_payload(payload)
    frame = bytearray(HEADER_SIZE + len(body))
    frame[0:4] = MAGIC
    frame[4] = VERSION
    frame[5] = direction
    frame[6] = codec
    frame[7] = 0
    frame[8:12] = sequence.to_bytes(4, "little")
    frame[12:20] = timestamp_us.to_bytes(8, "little")
    frame[20:24] = len(body).to_bytes(4, "little")
    frame[24:] = body
    return bytes(frame)


def unpack_frame(buffer: bytes | bytearray) -> AudioFrame:
    """Decode one complete frame. Partial frames are not buffered.

    Oversized input is rejected before the header is read. Error text does
    not include buffer contents.
    """
    if type(buffer) is not bytes and type(buffer) is not bytearray:
        raise AvipError("invalid_frame")
    if len(buffer) > MAX_FRAME_BYTES:
        raise AvipError("frame_too_large", "payload")
    if len(buffer) < HEADER_SIZE:
        raise AvipError("invalid_frame", "header")
    if bytes(buffer[0:4]) != MAGIC:
        raise AvipError("unsupported_protocol", "magic")
    if buffer[4] != VERSION:
        raise AvipError("unsupported_protocol", "version")
    if buffer[7] != 0:
        raise AvipError("invalid_frame", "reserved")
    direction = buffer[5]
    if direction not in _DIRECTIONS:
        raise AvipError("invalid_frame", "direction")
    codec = buffer[6]
    if codec != CODEC_PCM_S16LE:
        raise AvipError("unsupported_codec", "codec")
    sequence = int.from_bytes(buffer[8:12], "little")
    timestamp_us = int.from_bytes(buffer[12:20], "little")
    payload_length = int.from_bytes(buffer[20:24], "little")
    if payload_length > MAX_PAYLOAD_BYTES:
        raise AvipError("frame_too_large", "payload")
    payload = bytes(buffer[HEADER_SIZE:])
    if payload_length != len(payload):
        raise AvipError("invalid_frame", "payload")
    if len(payload) < SAMPLE_WIDTH_BYTES or len(payload) % SAMPLE_WIDTH_BYTES != 0:
        raise AvipError("invalid_frame", "payload")
    return AudioFrame(direction, sequence, timestamp_us, payload, codec)


def _require_direction(value: object) -> int:
    if type(value) is not int or value not in _DIRECTIONS:
        raise AvipError("invalid_frame", "direction")
    return value


def _require_codec(value: object) -> int:
    if type(value) is not int:
        raise AvipError("invalid_frame", "codec")
    if value != CODEC_PCM_S16LE:
        raise AvipError("unsupported_codec", "codec")
    return value


def _require_uint(value: object, upper: int, field: str) -> int:
    if type(value) is not int or not 0 <= value <= upper:
        raise AvipError("invalid_frame", field)
    return value


def _require_payload(value: object) -> bytes:
    if type(value) is not bytes and type(value) is not bytearray:
        raise AvipError("invalid_frame", "payload")
    length = len(value)
    if length > MAX_PAYLOAD_BYTES:
        raise AvipError("frame_too_large", "payload")
    if length < SAMPLE_WIDTH_BYTES or length % SAMPLE_WIDTH_BYTES != 0:
        raise AvipError("invalid_frame", "payload")
    return bytes(value)
