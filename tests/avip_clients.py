"""In-process AVIP clients that share one gateway.

``BrowserShapedClient`` is a test double. It builds JSON text and binary frames
with ``struct`` and does not call the gateway or ``avip_client``. The browser
adapter itself is ``clients/browser/avip-client.js``.

``HeadlessPythonClient`` is the installed headless SDK, ``avip_client``. It does
not call ``avi.protocols.media``. Neither client selects a gateway route.
"""
import json
from pathlib import Path
import struct

from avip_client import pack_frame, unpack_frame


_FIXTURES = Path(__file__).parent / "fixtures"
HANDSHAKE = json.loads((_FIXTURES / "avip-v1-handshake.json").read_text())
MEDIA = json.loads((_FIXTURES / "avip-v1-media.json").read_text())
_HEADER = struct.Struct("<4sBBBBIQI")


class BrowserShapedClient:
    """Text JSON plus one binary frame per chunk.

    The layout matches the browser adapter. This class is the in-process stand-in
    used by the gateway tests. Byte identity with the adapter is checked by
    running ``clients/browser/avip-client.js``.
    """

    kind = "browser"

    def __init__(self, message=None):
        self.message = json.loads(json.dumps(
            HANDSHAKE["valid"][0]["message"] if message is None else message
        ))
        self.negotiated = HANDSHAKE["valid"][0]["negotiated"]
        self.sequence = 0
        self.timestamp_us = 0

    def pack(self, direction: int, sequence: int, timestamp_us: int, payload: bytes) -> bytes:
        return _HEADER.pack(
            bytes.fromhex(MEDIA["magic_hex"]),
            MEDIA["version"],
            direction,
            MEDIA["codec"]["id"],
            0,
            sequence,
            timestamp_us,
            len(payload),
        ) + payload

    def pack_uplink(self, sequence: int, timestamp_us: int, payload: bytes) -> bytes:
        return self.pack(MEDIA["directions"]["audio_uplink"], sequence, timestamp_us, payload)

    def next_uplink(self, payload: bytes) -> bytes:
        raw = self.pack_uplink(self.sequence, self.timestamp_us, payload)
        self.sequence = (self.sequence + 1) % 2**32
        self.timestamp_us += MEDIA["nominal_chunk_us"]
        return raw

    def unpack(self, data: bytes) -> dict:
        if len(data) < MEDIA["header_bytes"]:
            raise ValueError("short frame")
        magic, version, direction, codec, reserved, sequence, timestamp_us, length = (
            _HEADER.unpack_from(data)
        )
        payload = data[MEDIA["header_bytes"]:]
        if (
            magic != bytes.fromhex(MEDIA["magic_hex"])
            or version != MEDIA["version"]
            or reserved != 0
            or codec != MEDIA["codec"]["id"]
            or length != len(payload)
        ):
            raise ValueError("bad frame")
        return {
            "direction": direction,
            "sequence": sequence,
            "timestamp_us": timestamp_us,
            "payload": payload,
        }


class HeadlessPythonClient:
    """Headless client. Framing goes through ``avip_client``."""

    kind = "headless"

    def __init__(self, message=None):
        self.message = json.loads(json.dumps(
            HANDSHAKE["valid"][1]["message"] if message is None else message
        ))
        self.negotiated = HANDSHAKE["valid"][1]["negotiated"]
        self.sequence = 0
        self.timestamp_us = 0

    def pack_uplink(self, sequence: int, timestamp_us: int, payload: bytes) -> bytes:
        return pack_frame(
            MEDIA["directions"]["audio_uplink"], sequence, timestamp_us, payload
        )

    def next_uplink(self, payload: bytes) -> bytes:
        raw = self.pack_uplink(self.sequence, self.timestamp_us, payload)
        self.sequence = (self.sequence + 1) % 2**32
        self.timestamp_us += MEDIA["nominal_chunk_us"]
        return raw

    def unpack(self, data: bytes):
        return unpack_frame(data)


def both_clients():
    return (BrowserShapedClient(), HeadlessPythonClient())


def payload_of(decoded) -> bytes:
    if isinstance(decoded, dict):
        return decoded["payload"]
    return decoded.payload


def direction_of(decoded) -> int:
    if isinstance(decoded, dict):
        return decoded["direction"]
    return int(decoded.direction)


def sequence_of(decoded) -> int:
    if isinstance(decoded, dict):
        return decoded["sequence"]
    return decoded.sequence


def timestamp_of(decoded) -> int:
    if isinstance(decoded, dict):
        return decoded["timestamp_us"]
    return decoded.timestamp_us
