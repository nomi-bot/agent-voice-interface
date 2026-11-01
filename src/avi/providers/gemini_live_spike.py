"""Credential-safe Gemini Live compatibility smoke.

This module is not a ``VoiceProvider``. ``GeminiLiveProvider`` still raises
``NotImplementedError``. A live session is opened only when ``GEMINI_API_KEY``
and ``GEMINI_LIVE_MODEL`` are both set. There is no default model id. The
decision record is ``docs/adr/011-gemini-live-compatibility.md``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import os
import sys

SDK_PIN = "2.34.0"
INPUT_SAMPLE_RATE_HZ = 16_000
OUTPUT_SAMPLE_RATE_HZ = 24_000
_MODEL_TOKEN_MAX = 128


class ModelUnconfigured(Exception):
    """A key is present and the caller did not supply a model code."""


class SdkPinError(Exception):
    """The installed google-genai distribution is missing or not the pinned one."""


class SmokeStatus:
    SKIPPED = "skipped"
    READY = "ready"


@dataclass(frozen=True)
class _Redacted:
    value: str

    def __repr__(self) -> str:
        return f"_Redacted(len={len(self.value)})"

    __str__ = __repr__


@dataclass(frozen=True)
class SmokePlan:
    status: str
    reason: str
    model: str | None = None
    api_key: _Redacted | None = None


@dataclass(frozen=True)
class ServerSignal:
    """Public fields of one server message. Resumption handles are not copied."""

    kind: str
    cancellation_ids: tuple[str, ...] = ()
    go_away: bool = False
    tool_call: bool = False
    interrupted: bool = False
    turn_complete: bool = False
    turn_complete_reason: str | None = None
    audio_bytes: int = 0
    transcript: bool = False
    resumption_handle_present: bool = False


@dataclass(frozen=True)
class SmokeReport:
    status: str
    reason: str
    model: str | None = None
    audio_bytes: int = 0
    saw_turn_complete: bool = False
    saw_transcript: bool = False
    resumption_handle_present: bool = False


def _nonempty(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError("environment values must be strings")
    stripped = value.strip()
    return stripped or None


def _model_token(value: str) -> str:
    if len(value) > _MODEL_TOKEN_MAX:
        raise ModelUnconfigured("GEMINI_LIVE_MODEL is not a single model-code token")
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")
    if any(character not in allowed for character in value):
        raise ModelUnconfigured("GEMINI_LIVE_MODEL is not a single model-code token")
    if value[0] not in set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"):
        raise ModelUnconfigured("GEMINI_LIVE_MODEL is not a single model-code token")
    return value


def plan_smoke(env: Mapping[str, str]) -> SmokePlan:
    """Decide whether a live call is allowed.

    ``GEMINI_API_KEY`` is the only accepted credential. ``GOOGLE_API_KEY`` and
    application-default credentials are ignored. A blank key skips. A key
    without ``GEMINI_LIVE_MODEL`` fails closed.
    """

    key = _nonempty(env.get("GEMINI_API_KEY"))
    if key is None:
        return SmokePlan(
            status=SmokeStatus.SKIPPED,
            reason="GEMINI_API_KEY is unset; live call skipped",
        )
    model = _nonempty(env.get("GEMINI_LIVE_MODEL"))
    if model is None:
        raise ModelUnconfigured(
            "GEMINI_API_KEY is set but GEMINI_LIVE_MODEL is unset; "
            "refusing to choose a model"
        )
    return SmokePlan(
        status=SmokeStatus.READY,
        reason="GEMINI_API_KEY and GEMINI_LIVE_MODEL are set",
        model=_model_token(model),
        api_key=_Redacted(key),
    )


def redact(text: str, secret: str | None) -> str:
    cleaned = " ".join(text.split())
    if secret:
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def validate_pcm_s16le(payload: bytes) -> None:
    if type(payload) is not bytes or len(payload) == 0 or len(payload) % 2 != 0:
        raise ValueError("PCM payload must be non-empty even-length bytes")


def pcm_input_blob(
    *, data: bytes, sample_rate_hz: int = INPUT_SAMPLE_RATE_HZ
) -> dict[str, bytes | str]:
    """Shape one Live API input blob. The caller sends it; this function does not."""

    validate_pcm_s16le(data)
    if type(sample_rate_hz) is not int or sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be a positive integer")
    return {"data": data, "mime_type": f"audio/pcm;rate={sample_rate_hz}"}


def assert_sdk_pin() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        found = version("google-genai")
    except PackageNotFoundError as exc:
        raise SdkPinError(
            "google-genai is not installed; install the gemini-live extra"
        ) from exc
    if found != SDK_PIN:
        raise SdkPinError(f"google-genai {found} does not match pin {SDK_PIN}")
    from google.genai.live import AsyncSession

    required = (
        "send_client_content",
        "send_realtime_input",
        "send_tool_response",
        "receive",
        "close",
    )
    missing = [name for name in required if not callable(getattr(AsyncSession, name, None))]
    if missing:
        raise SdkPinError(
            "pinned google-genai is missing Live session methods: " + ", ".join(missing)
        )
    return found


def native_audio_config():
    """Session config for a native-audio Live turn. The model id is not included."""

    assert_sdk_pin()
    from google.genai import types

    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        session_resumption=types.SessionResumptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return str(raw)


def read_signal(message: object) -> ServerSignal:
    """Classify one server message without retaining secrets or audio bytes."""

    cancellation = getattr(message, "tool_call_cancellation", None)
    go_away = getattr(message, "go_away", None)
    tool_call = getattr(message, "tool_call", None)
    content = getattr(message, "server_content", None)
    update = getattr(message, "session_resumption_update", None)
    data = getattr(message, "data", None)
    audio_bytes = len(data) if type(data) is bytes else 0
    interrupted = bool(content is not None and getattr(content, "interrupted", False))
    turn_complete = bool(content is not None and getattr(content, "turn_complete", False))
    transcript = getattr(content, "output_transcription", None) if content is not None else None
    transcript_text = getattr(transcript, "text", None) if transcript is not None else None
    ids: tuple[str, ...] = ()
    if cancellation is not None:
        raw_ids = getattr(cancellation, "ids", None) or ()
        ids = tuple(str(item) for item in raw_ids)
    handle_present = bool(
        update is not None
        and getattr(update, "resumable", False)
        and getattr(update, "new_handle", None)
    )
    if cancellation is not None:
        kind = "tool_call_cancellation"
    elif go_away is not None:
        kind = "go_away"
    elif tool_call is not None:
        kind = "tool_call"
    elif interrupted:
        kind = "interrupted"
    elif turn_complete:
        kind = "turn_complete"
    elif audio_bytes:
        kind = "audio"
    else:
        kind = "other"
    return ServerSignal(
        kind=kind,
        cancellation_ids=ids,
        go_away=go_away is not None,
        tool_call=tool_call is not None,
        interrupted=interrupted,
        turn_complete=turn_complete,
        turn_complete_reason=(
            _enum_value(getattr(content, "turn_complete_reason", None))
            if content is not None
            else None
        ),
        audio_bytes=audio_bytes,
        transcript=bool(transcript_text),
        resumption_handle_present=handle_present,
    )


def _successful_turn(reason: str | None, audio_bytes: int, transcript: bool) -> str | None:
    if reason not in (None, "TURN_COMPLETE_REASON_UNSPECIFIED"):
        return f"turn_complete_reason={reason}"
    if audio_bytes <= 0 and not transcript:
        return "turn completed without audio or output transcript"
    return None


async def run_live_smoke(
    env: Mapping[str, str], *, timeout_s: float = 30.0
) -> SmokeReport:
    """Open one short Live turn, or skip when ``GEMINI_API_KEY`` is unset."""

    plan = plan_smoke(env)
    if plan.status == SmokeStatus.SKIPPED:
        return SmokeReport(status="skipped", reason=plan.reason)
    assert plan.api_key is not None and plan.model is not None
    assert_sdk_pin()
    from google import genai

    secret = plan.api_key.value
    audio_bytes = 0
    saw_turn = False
    saw_transcript = False
    handle_present = False
    turn_reason: str | None = None
    try:
        async with asyncio.timeout(timeout_s):
            client = genai.Client(api_key=secret, vertexai=False)
            async with client.aio.live.connect(
                model=plan.model, config=native_audio_config()
            ) as session:
                await session.send_client_content(
                    turns={
                        "role": "user",
                        "parts": [
                            {"text": "Reply with one short spoken word: hello."}
                        ],
                    },
                    turn_complete=True,
                )
                async for message in session.receive():
                    signal = read_signal(message)
                    audio_bytes += signal.audio_bytes
                    saw_transcript = saw_transcript or signal.transcript
                    handle_present = handle_present or signal.resumption_handle_present
                    if signal.tool_call:
                        return SmokeReport(
                            status="failed",
                            reason="unexpected tool call",
                            model=plan.model,
                            audio_bytes=audio_bytes,
                        )
                    if signal.kind == "tool_call_cancellation":
                        return SmokeReport(
                            status="failed",
                            reason="unexpected tool cancellation",
                            model=plan.model,
                            audio_bytes=audio_bytes,
                        )
                    if signal.go_away:
                        return SmokeReport(
                            status="failed",
                            reason="go_away before turn_complete",
                            model=plan.model,
                            audio_bytes=audio_bytes,
                            saw_transcript=saw_transcript,
                            resumption_handle_present=handle_present,
                        )
                    if signal.turn_complete:
                        saw_turn = True
                        turn_reason = signal.turn_complete_reason
                        break
    except Exception as exc:
        return SmokeReport(
            status="failed",
            reason=redact(f"{type(exc).__name__}: {exc}", secret),
            model=plan.model,
            audio_bytes=audio_bytes,
            saw_turn_complete=saw_turn,
            saw_transcript=saw_transcript,
            resumption_handle_present=handle_present,
        )
    if not saw_turn:
        return SmokeReport(
            status="failed",
            reason="stream ended before turn_complete",
            model=plan.model,
            audio_bytes=audio_bytes,
            saw_transcript=saw_transcript,
            resumption_handle_present=handle_present,
        )
    problem = _successful_turn(turn_reason, audio_bytes, saw_transcript)
    if problem is not None:
        return SmokeReport(
            status="failed",
            reason=problem,
            model=plan.model,
            audio_bytes=audio_bytes,
            saw_turn_complete=True,
            saw_transcript=saw_transcript,
            resumption_handle_present=handle_present,
        )
    return SmokeReport(
        status="passed",
        reason="live turn completed",
        model=plan.model,
        audio_bytes=audio_bytes,
        saw_turn_complete=True,
        saw_transcript=saw_transcript,
        resumption_handle_present=handle_present,
    )


def main(env: Mapping[str, str] | None = None) -> int:
    """Run the smoke from the command line.

    Exit 0 is a completed live turn. Exit 2 means the key was absent. Exit 3
    means the key was set and the model was not. Exit 4 means the SDK pin is
    not installed. Exit 1 is any other live failure.
    """

    selected: Mapping[str, str] = os.environ if env is None else env
    try:
        report = asyncio.run(run_live_smoke(selected))
    except ModelUnconfigured as exc:
        print(f"status=failed reason={exc}", file=sys.stderr)
        return 3
    except SdkPinError as exc:
        print(f"status=failed reason={exc}", file=sys.stderr)
        return 4
    if report.status == "skipped":
        print(f"status=skipped reason={report.reason}", file=sys.stderr)
        return 2
    if report.status == "passed":
        print(
            "status=passed "
            f"model={report.model} "
            f"audio_bytes={report.audio_bytes} "
            f"transcript={str(report.saw_transcript).lower()} "
            f"resumption_handle={str(report.resumption_handle_present).lower()}"
        )
        return 0
    print(
        f"status=failed model={report.model} reason={report.reason}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
