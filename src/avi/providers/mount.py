"""Choose the `/ws/avip` voice path without opening a socket.

Complete Live credentials win. ``AVIP_MOCK_MEDIA=1`` is the fallback when the
key or the model is missing, and it is also what local demos use when Live
is intentionally not configured. A key without a single model token does not
select a model and does not call the network.

The result does not carry the key, the model id, or any audio.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from avi.protocols.avip import Capabilities
from avi.providers.gemini_live_spike import ModelUnconfigured, SmokeStatus, plan_smoke


MODE_LIVE = "live"
MODE_MOCK = "mock"
MODE_NOT_CONFIGURED = "not_configured"

REASON_CONFIGURED = "configured"
REASON_MOCK_MEDIA = "mock_media"
REASON_MODEL_UNCONFIGURED = "model_unconfigured"
REASON_CREDENTIALS_ABSENT = "credentials_absent"

_AUDIO = Capabilities(audio_input=True, audio_output=True)
_NONE = Capabilities()


@dataclass(frozen=True)
class VoiceMount:
    """What `/ws/avip` should do for one process environment.

    ``mode`` is ``live``, ``mock``, or ``not_configured``. ``reason`` is a
    short token with no credential and no model id. ``capabilities`` are the
    bits the gateway can honestly advertise for that mode.
    """

    mode: str
    reason: str
    capabilities: Capabilities


@dataclass(frozen=True)
class SessionVoice:
    """Provider and telemetry for one `/ws/avip` connection.

    ``provider`` is None on the mock and unconfigured paths. The object does
    not store a key or a model id.
    """

    provider: object | None
    capabilities: Capabilities
    telemetry_path: str
    telemetry_scenario: str
    mount: VoiceMount
    elicit: str | None = None


def attach_voice(
    unconfigured: Capabilities, environ: Mapping[str, str] | None = None,
) -> SessionVoice:
    """Build the voice path for one connection.

    ``unconfigured`` is used only when Live is not configured and the mock
    flag is off, so tests can advertise a partial capability set. Live and
    mock ignore it.
    """

    mount = resolve_voice_mount(environ)
    if mount.mode == MODE_LIVE:
        from avi.providers.gemini_live import GeminiLiveProvider

        return SessionVoice(
            GeminiLiveProvider(),
            mount.capabilities,
            "provider",
            "gemini-live",
            mount,
            _elicit(environ if environ is not None else os.environ),
        )
    if mount.mode == MODE_MOCK:
        return SessionVoice(None, mount.capabilities, "mock-mirror", "gateway", mount)
    return SessionVoice(None, unconfigured, "mock-mirror", "gateway", mount)


def _elicit(environ: Mapping[str, str]) -> str | None:
    """Optional server-side text turn. The value is not logged."""

    raw = environ.get("AVIP_LIVE_ELICIT")
    if type(raw) is not str:
        return None
    text = raw.strip()
    if not text or len(text) > 200:
        return None
    return text


def resolve_voice_mount(environ: Mapping[str, str] | None = None) -> VoiceMount:
    """Return the voice path for ``environ``, or for the process environment.

    This reads ``GEMINI_API_KEY``, ``GEMINI_LIVE_MODEL``, and
    ``AVIP_MOCK_MEDIA``. ``GOOGLE_API_KEY`` is ignored. Nothing is stored.
    """

    source = os.environ if environ is None else environ
    failure = _live_failure(source)
    if failure is None:
        return VoiceMount(MODE_LIVE, REASON_CONFIGURED, _AUDIO)
    if source.get("AVIP_MOCK_MEDIA") == "1":
        reason = (
            REASON_MODEL_UNCONFIGURED
            if failure == REASON_MODEL_UNCONFIGURED
            else REASON_MOCK_MEDIA
        )
        return VoiceMount(MODE_MOCK, reason, _AUDIO)
    if failure == REASON_MODEL_UNCONFIGURED:
        return VoiceMount(MODE_NOT_CONFIGURED, REASON_MODEL_UNCONFIGURED, _NONE)
    return VoiceMount(MODE_NOT_CONFIGURED, REASON_CREDENTIALS_ABSENT, _NONE)


def _live_failure(environ: Mapping[str, str]) -> str | None:
    """Return None when Live is fully configured. The key is not retained."""

    try:
        plan = plan_smoke(environ)
    except ModelUnconfigured:
        return REASON_MODEL_UNCONFIGURED
    except TypeError:
        return REASON_CREDENTIALS_ABSENT
    if plan.status == SmokeStatus.READY:
        return None
    return REASON_CREDENTIALS_ABSENT
