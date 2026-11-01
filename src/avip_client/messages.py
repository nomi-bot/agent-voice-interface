"""Canonical ``session.start`` JSON for the headless client.

The browser adapter builds the same text. Key order is fixed, separators are
compact, and non-ASCII capability names are written as UTF-8. Unknown
capability names sort by UTF-16 code units, which is the order JavaScript
uses when it compares strings.
"""

from collections.abc import Mapping
import json
import re

from avip_client.errors import AvipError


PROTOCOL = "avip/1"
SESSION_START = "session.start"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ENVELOPE = ("protocol", "type", "request_id", "session_id", "device_id", "capabilities")
_CAPABILITIES = ("audio_input", "audio_output", "camera", "display", "robot_actions")


def session_start(
    *,
    request_id: str,
    device_id: str,
    capabilities: Mapping[str, bool],
) -> str:
    """Return the canonical text of one ``session.start`` message."""
    return encode_session_start({
        "protocol": PROTOCOL,
        "type": SESSION_START,
        "request_id": request_id,
        "session_id": None,
        "device_id": device_id,
        "capabilities": dict(capabilities),
    })


def encode_session_start(message: Mapping[str, object]) -> str:
    """Validate ``message`` and return its canonical JSON text.

    Omitted capability names stay omitted. Unknown boolean names are kept, in
    UTF-16 code-unit order after the known names, so a newer client can still
    be compared byte for byte with the browser adapter. The gateway ignores
    those unknown names.
    """
    if type(message) is not dict or set(message) != set(_ENVELOPE):
        raise AvipError("invalid_message")
    if message["protocol"] != PROTOCOL:
        raise AvipError("unsupported_protocol", "protocol")
    if message["type"] != SESSION_START:
        raise AvipError("unsupported_message_type", "type")
    if message["session_id"] is not None:
        raise AvipError("invalid_message", "session_id")
    body = {
        "protocol": PROTOCOL,
        "type": SESSION_START,
        "request_id": _identifier(message["request_id"], "request_id"),
        "session_id": None,
        "device_id": _identifier(message["device_id"], "device_id"),
        "capabilities": _capabilities(message["capabilities"]),
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def parse_server_text(raw: str) -> dict:
    """Decode one server text frame. The result is the JSON object."""
    if type(raw) is not str:
        raise AvipError("invalid_message")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise AvipError("invalid_message") from None
    if type(value) is not dict:
        raise AvipError("invalid_message")
    return value


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise AvipError("invalid_message", field)
    return value


def _capabilities(value: object) -> dict[str, bool]:
    if type(value) is not dict or any(
        type(name) is not str or type(flag) is not bool for name, flag in value.items()
    ):
        raise AvipError("invalid_message", "capabilities")
    ordered: dict[str, bool] = {}
    for name in _CAPABILITIES:
        if name in value:
            ordered[name] = value[name]
    extras = sorted(set(value) - set(_CAPABILITIES), key=lambda name: name.encode("utf-16-be"))
    for name in extras:
        ordered[name] = value[name]
    return ordered
