"""AVIP v1 session handshake.

The pure helpers below do not open sockets. ``/ws/avip`` uses them; the legacy
``/ws`` control protocol does not.
"""

from dataclasses import asdict, dataclass, fields
import re


PROTOCOL = "avip/1"
SESSION_START = "session.start"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_START_FIELDS = {
    "protocol", "type", "request_id", "session_id", "device_id", "capabilities",
}
_ERROR_CODES = frozenset({
    "invalid_message",
    "unsupported_protocol",
    "unsupported_message_type",
    "session_already_started",
})
_ERROR_FIELDS = frozenset(_START_FIELDS)


class ProtocolError(ValueError):
    """Stable validation failure without reflecting untrusted message contents."""

    def __init__(self, code: str, field: str | None = None):
        self.code = code
        self.field = field
        super().__init__(code if field is None else f"{code}: {field}")


@dataclass(frozen=True)
class Capabilities:
    """Optional features, disabled unless both peers advertise support.

    Negotiation is not authorization. In v0.1, robot_actions can describe only
    mock/simulated actions; no negotiated bit grants physical actuation.
    """

    audio_input: bool = False
    audio_output: bool = False
    camera: bool = False
    display: bool = False
    robot_actions: bool = False


_CAPABILITY_NAMES = tuple(field.name for field in fields(Capabilities))


@dataclass(frozen=True)
class SessionStart:
    request_id: str
    device_id: str
    capabilities: Capabilities


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ProtocolError("invalid_message", field)
    return value


def parse_session_start(message: object) -> SessionStart:
    """Validate an already-decoded JSON handshake into an immutable value.

    Unknown envelope fields fail closed. Unknown optional boolean capabilities
    are ignored, so newer clients cannot enable features on an older gateway.
    Device identifiers are labels, never authenticated identities.
    """
    if not isinstance(message, dict) or set(message) != _START_FIELDS:
        raise ProtocolError("invalid_message")
    if message["protocol"] != PROTOCOL:
        raise ProtocolError("unsupported_protocol", "protocol")
    if message["type"] != SESSION_START:
        raise ProtocolError("unsupported_message_type", "type")
    if message["session_id"] is not None:
        raise ProtocolError("invalid_message", "session_id")
    request_id = _identifier(message["request_id"], "request_id")
    device_id = _identifier(message["device_id"], "device_id")
    capabilities = message["capabilities"]
    if not isinstance(capabilities, dict) or any(
        not isinstance(name, str) or type(value) is not bool
        for name, value in capabilities.items()
    ):
        raise ProtocolError("invalid_message", "capabilities")
    return SessionStart(
        request_id=request_id,
        device_id=device_id,
        capabilities=Capabilities(**{
            name: capabilities.get(name, False) for name in _CAPABILITY_NAMES
        }),
    )


def is_session_start(message: object) -> bool:
    """Return whether this value is a session-start attempt.

    This checks only ``type``. It does not validate the envelope. A second
    attempt on an open connection is rejected without being applied.
    """
    return isinstance(message, dict) and message.get("type") == SESSION_START


def error_envelope(error: ProtocolError) -> dict:
    """Build a server-authored error envelope.

    ``code`` and ``field`` are copied only from closed allowlists. Exception
    text and every client-supplied value are omitted. ``session_already_started``
    is a connection-lifecycle code; the parser does not raise it.
    """
    code = error.code if isinstance(error.code, str) and error.code in _ERROR_CODES else None
    field = error.field if isinstance(error.field, str) and error.field in _ERROR_FIELDS else None
    return {
        "protocol": PROTOCOL,
        "type": "error",
        "code": error.code if code is not None else "invalid_message",
        "field": field if code is not None else None,
    }


def negotiate_capabilities(requested: Capabilities, available: Capabilities) -> Capabilities:
    """Select only features requested by the client and supported by the gateway."""
    return Capabilities(**{
        name: getattr(requested, name) and getattr(available, name)
        for name in _CAPABILITY_NAMES
    })


def session_ready(start: SessionStart, session_id: str, available: Capabilities) -> dict:
    """Build a correlated reply using an identifier allocated by the gateway.

    This pure contract helper does not create a live session or wire up a provider.
    Callers must advertise actual enabled support, not planned capabilities.
    """
    return {
        "protocol": PROTOCOL,
        "type": "session.ready",
        "request_id": start.request_id,
        "session_id": _identifier(session_id, "session_id"),
        "capabilities": asdict(negotiate_capabilities(start.capabilities, available)),
    }
