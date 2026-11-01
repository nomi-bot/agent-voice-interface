"""Headless AVIP v1 client.

Import this package to speak ``/ws/avip`` without importing the gateway.
Framing and ``session.start`` text are implemented here. The browser adapter
in ``clients/browser/avip-client.js`` implements them again. Conformance
checks those two encoders against each other.
"""

from avip_client.errors import AvipError
from avip_client.framing import (
    AUDIO_DOWNLINK,
    AUDIO_UPLINK,
    NOMINAL_CHUNK_US,
    NOMINAL_PAYLOAD_BYTES,
    AudioFrame,
    UplinkClock,
    pack_frame,
    unpack_frame,
)
from avip_client.messages import encode_session_start, session_start
from avip_client.session import AvipClient

__all__ = [
    "AUDIO_DOWNLINK",
    "AUDIO_UPLINK",
    "NOMINAL_CHUNK_US",
    "NOMINAL_PAYLOAD_BYTES",
    "AudioFrame",
    "AvipClient",
    "AvipError",
    "UplinkClock",
    "encode_session_start",
    "pack_frame",
    "session_start",
    "unpack_frame",
]
