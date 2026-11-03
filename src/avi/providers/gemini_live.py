"""Gemini Live adapter for the ``VoiceProvider`` port.

The gateway does not construct this class. ``/ws/avip`` still drains uplink
with ``MockMediaSink`` and mirrors 16 kHz PCM. A process that wants a Live
session constructs ``GeminiLiveProvider`` itself and keeps the API key in the
server environment.

``GEMINI_API_KEY`` and ``GEMINI_LIVE_MODEL`` follow the spike: a missing key
does not call the network, a key without a model fails closed, and
``GOOGLE_API_KEY`` is not a credential. Model ids are not embedded here.
Resumption handles are not stored. Audio and keys are not written.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
import logging
import os
from typing import Any

from avi.core.events import VoiceEvent
from avi.providers.base import VoiceCapabilities
from avi.providers.gemini_live_spike import (
    INPUT_SAMPLE_RATE_HZ,
    OUTPUT_SAMPLE_RATE_HZ,
    ModelUnconfigured,
    SmokeStatus,
    assert_sdk_pin,
    plan_smoke,
    redact,
)
from avi.providers.resample import StreamingResampler


logger = logging.getLogger(__name__)

AVIP_SAMPLE_RATE_HZ = INPUT_SAMPLE_RATE_HZ
TOOL_NAME = "avi_ack"
# There is no Live RPC named cancel_response. This client content message
# is the interrupt the adapter sends. A credentialed smoke can confirm the
# selected model accepts it; this module does not embed a model id.
CANCEL_SIGNAL = "send_client_content(turns=None, turn_complete=True)"
_TOOL_BEHAVIORS = frozenset({"BLOCKING", "NON_BLOCKING"})
_MAX_UPLINK_CHUNKS = 8
_MAX_UPLINK_CHUNK_BYTES = 32_000
_MAX_AUDIO_EVENTS = 8
_MAX_CONTROL_EVENTS = 32
_MAX_TOOL_RESULT_BYTES = 16_384
_MAX_PENDING_TOOL_CALLS = 16
_TEXT_TURN_MAX = 2_000

_END = object()


class ProviderNotConfigured(Exception):
    """The Live credential is absent. No network call was made."""


class ProviderSessionError(Exception):
    """The Live session failed. The message is already redacted."""


@dataclass(frozen=True)
class DuplexReport:
    status: str
    reason: str
    model: str | None = None
    uplink_bytes: int = 0
    downlink_bytes: int = 0
    first_audio_ms: int | None = None
    saw_turn_complete: bool = False
    cancel_signal: str | None = None
    cancel_accepted: bool | None = None


@dataclass(frozen=True)
class ToolBehaviorReport:
    behavior: str
    status: str
    reason: str
    scheduling: str | None = None
    saw_tool_call: bool = False
    audio_before_tool_response: bool = False
    audio_after_tool_response: bool = False
    saw_turn_complete: bool = False


@dataclass(frozen=True)
class ToolHandshakeReport:
    status: str
    reason: str
    model: str | None = None
    behaviors: tuple[ToolBehaviorReport, ...] = ()


class _ChunkQueue:
    def __init__(self, max_chunks: int):
        self._items: deque[bytes] = deque()
        self._max = max_chunks
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._items)

    def put(self, item: bytes) -> bool:
        if self._closed:
            raise RuntimeError("provider is closed")
        if len(self._items) >= self._max:
            self.dropped += 1
            return False
        self._items.append(item)
        self._idle.clear()
        self._wake.set()
        return True

    async def get(self) -> bytes | None:
        while not self._items:
            if self._closed:
                return None
            self._wake.clear()
            if self._items or self._closed:
                continue
            await self._wake.wait()
        return self._items.popleft()

    def mark_idle_if_empty(self) -> None:
        if not self._items:
            self._idle.set()

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def close(self) -> None:
        self._closed = True
        self._items.clear()
        self._idle.set()
        self._wake.set()


class _EventQueue:
    def __init__(self, max_audio: int, max_control: int):
        self._items: deque[VoiceEvent | object] = deque()
        self._max_audio = max_audio
        self._max_control = max_control
        self._audio = 0
        self._control = 0
        self._wake = asyncio.Event()
        self._closed = False
        self.overflowed = False
        self.dropped_audio = 0
        self.flushed_audio = 0

    def push_audio(self, payload: bytes) -> bool:
        if self._closed or self.overflowed:
            return False
        if self._audio >= self._max_audio:
            self.dropped_audio += 1
            return False
        self._items.append(VoiceEvent(kind="audio", payload=payload))
        self._audio += 1
        self._wake.set()
        return True

    def push_control(self, event: VoiceEvent) -> bool:
        if self._closed:
            return False
        if self.overflowed:
            return False
        if self._control >= self._max_control:
            self._overflow()
            return False
        self._items.append(event)
        self._control += 1
        self._wake.set()
        return True

    def clear_audio(self) -> None:
        kept: deque[VoiceEvent | object] = deque()
        control = 0
        for item in self._items:
            if item is _END or not isinstance(item, VoiceEvent) or item.kind != "audio":
                kept.append(item)
                if isinstance(item, VoiceEvent):
                    control += 1
            else:
                self.flushed_audio += 1
        self._items = kept
        self._audio = 0
        self._control = control

    def finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._items.append(_END)
        self._wake.set()

    async def get(self) -> VoiceEvent | None:
        while not self._items:
            if self._closed:
                return None
            self._wake.clear()
            if self._items or self._closed:
                continue
            await self._wake.wait()
        item = self._items.popleft()
        if item is _END:
            return None
        if not isinstance(item, VoiceEvent):
            return None
        if item.kind == "audio":
            self._audio -= 1
        else:
            self._control -= 1
        return item

    def _overflow(self) -> None:
        self.overflowed = True
        self._items.clear()
        self._audio = 0
        self._control = 0
        self._items.append(
            VoiceEvent(kind="error", payload={"code": "event_overflow"})
        )
        self._control = 1
        self._wake.set()


def live_connect_config(tool_behavior: str):
    """Native-audio Live config. The model id is not included.

    Session resumption is left unset. The adapter closes on GoAway instead of
    storing a handle. Thinking, affective dialog, and proactivity stay unset.
    """

    if tool_behavior not in _TOOL_BEHAVIORS:
        raise ValueError("tool behavior must be BLOCKING or NON_BLOCKING")
    assert_sdk_pin()
    from google.genai import types

    behavior = (
        types.Behavior.BLOCKING
        if tool_behavior == "BLOCKING"
        else types.Behavior.NON_BLOCKING
    )
    declaration = types.FunctionDeclaration(
        name=TOOL_NAME,
        description=(
            "Acknowledge a voice-session request. "
            "No device control and no robot action."
        ),
        parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        behavior=behavior,
    )
    return types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=[types.Tool(function_declarations=[declaration])],
    )


class GeminiLiveProvider:
    """Live API session behind ``VoiceProvider``.

    ``send_audio`` accepts 16 kHz mono PCM s16le, the AVIP uplink profile.
    ``events`` yields that same profile: 24 kHz provider output is resampled
    with one ``StreamingResampler`` for the life of the session.     ``cancel_response`` sends ``CANCEL_SIGNAL``. Queued audio is dropped, and
    later chunks from that generation are dropped until ``turn_complete``.
    The resampler tail is discarded at that boundary, before the event. The
    next turn is emitted. This does not cancel an agent run.

    Not safe to share across threads. One ``events`` consumer.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        connector: Callable[[], Awaitable[object]] | None = None,
        tool_behavior: str = "NON_BLOCKING",
        output_rate_hz: int = OUTPUT_SAMPLE_RATE_HZ,
        max_uplink_chunks: int = _MAX_UPLINK_CHUNKS,
        max_audio_events: int = _MAX_AUDIO_EVENTS,
        max_control_events: int = _MAX_CONTROL_EVENTS,
    ):
        if tool_behavior not in _TOOL_BEHAVIORS:
            raise ValueError("tool behavior must be BLOCKING or NON_BLOCKING")
        if type(output_rate_hz) is not int or output_rate_hz <= 0:
            raise ValueError("output_rate_hz must be a positive integer")
        self.capabilities = VoiceCapabilities(
            full_duplex=True,
            tool_calls=True,
            response_cancellation=True,
        )
        self.tool_behavior = tool_behavior
        self.output_rate_hz = output_rate_hz
        self.model: str | None = None
        self.sent_uplink_bytes = 0
        self.dropped_uplink_chunks = 0
        self.dropped_audio_events = 0
        self.flushed_audio_events = 0
        self.resumption_updates = 0
        self._env = os.environ if env is None else env
        self._connector = connector
        self._uplink = _ChunkQueue(max_uplink_chunks)
        self._events = _EventQueue(max_audio_events, max_control_events)
        self._resampler = StreamingResampler(output_rate_hz, AVIP_SAMPLE_RATE_HZ)
        self._source_rate = output_rate_hz
        self._seen_audio = False
        self._suppress_audio = False
        self.suppressed_audio_chunks = 0
        self._pending_tools: dict[str, str] = {}
        self._generation_complete = False
        self._interaction_status: str | None = None
        self._secret: str | None = None
        self._client: object | None = None
        self._cm: object | None = None
        self._session: object | None = None
        self._reader: asyncio.Task[None] | None = None
        self._sender: asyncio.Task[None] | None = None
        self._state = "idle"
        self._subscribed = False
        self._shutdown_started = False
        self._shutdown_done = asyncio.Event()
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"GeminiLiveProvider(state={self._state}, model={self.model})"

    async def start(self) -> None:
        async with self._lock:
            if self._state != "idle":
                raise RuntimeError("provider already started")
            self._state = "starting"
        try:
            session = await self._connect()
        except (ProviderNotConfigured, ModelUnconfigured):
            self._fail_start()
            raise
        except ProviderSessionError:
            self._fail_start()
            raise
        except Exception as exc:
            message = self._safe(exc)
            self._secret = None
            self._fail_start()
            raise ProviderSessionError(message) from None
        async with self._lock:
            if self._shutdown_started:
                self._session = session
                await self._close_session()
                self._state = "closed"
                self._shutdown_done.set()
                raise ProviderSessionError("provider closed during connect")
            self._session = session
            self._sender = asyncio.create_task(self._send_loop(), name="avi-live-uplink")
            self._reader = asyncio.create_task(self._read_loop(), name="avi-live-downlink")
            self._state = "open"

    async def send_audio(self, pcm: bytes) -> None:
        if type(pcm) is not bytes or len(pcm) == 0 or len(pcm) % 2 != 0:
            raise ValueError("PCM payload must be non-empty even-length bytes")
        if len(pcm) > _MAX_UPLINK_CHUNK_BYTES:
            raise ValueError("PCM payload exceeds the uplink chunk bound")
        if self._state != "open":
            raise RuntimeError("provider is not open")
        self._uplink.put(pcm)
        self.dropped_uplink_chunks = self._uplink.dropped

    def events(self) -> AsyncIterator[VoiceEvent]:
        if self._subscribed:
            raise RuntimeError("events already subscribed")
        self._subscribed = True
        return self._iterate_events()

    async def tool_result(self, call_id: str, result: dict) -> None:
        if type(call_id) is not str or not call_id:
            raise ValueError("call_id must be a non-empty string")
        body = _tool_response_body(result)
        if self._state != "open" or self._session is None:
            raise RuntimeError("provider is not open")
        name = self._pending_tools.pop(call_id, None)
        if name is None:
            raise ValueError("unknown tool call")
        response: dict[str, Any] = {"id": call_id, "name": name, "response": body}
        if self.tool_behavior == "NON_BLOCKING":
            response["scheduling"] = "WHEN_IDLE"
        failure: str | None = None
        try:
            await self._session.send_tool_response(function_responses=response)  # type: ignore[attr-defined]
        except Exception as exc:
            self._pending_tools[call_id] = name
            failure = await self._fail(exc)
        if failure is not None:
            raise ProviderSessionError(failure)

    async def cancel_response(self) -> None:
        """Interrupt the current generation.

        The Live API has no ``cancel_response`` method. The signal is
        ``CANCEL_SIGNAL``: client content with no turn and ``turn_complete``
        true. Audio already queued is discarded. Chunks that arrive afterward
        belong to the cancelled generation until ``turn_complete``, and they
        are not emitted. The following turn is.
        """

        if self._state != "open" or self._session is None:
            raise RuntimeError("provider is not open")
        self._hold_generation()
        failure: str | None = None
        try:
            await self._session.send_client_content(turns=None, turn_complete=True)  # type: ignore[attr-defined]
        except Exception as exc:
            failure = await self._fail(exc)
        if failure is not None:
            raise ProviderSessionError(failure)
        self._events.push_control(
            VoiceEvent(
                kind="interrupted",
                payload={"source": "cancel", "signal": CANCEL_SIGNAL},
            )
        )

    async def close(self) -> None:
        await self._shutdown()

    async def _submit_text(self, text: str) -> None:
        """One completed text turn. Not part of ``VoiceProvider``."""

        if type(text) is not str or not text.strip() or len(text) > _TEXT_TURN_MAX:
            raise ValueError("text turn must be a short non-empty string")
        if self._state != "open" or self._session is None:
            raise RuntimeError("provider is not open")
        await self._session.send_client_content(  # type: ignore[attr-defined]
            turns={"role": "user", "parts": [{"text": text}]},
            turn_complete=True,
        )

    async def _wait_uplink_idle(self) -> None:
        await self._uplink.wait_idle()

    async def _iterate_events(self) -> AsyncIterator[VoiceEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def _connect(self) -> object:
        if self._connector is not None:
            self._remember_redaction_key()
            return await self._connector()
        plan = plan_smoke(self._env)
        if plan.status == SmokeStatus.SKIPPED:
            raise ProviderNotConfigured("GEMINI_API_KEY is unset")
        assert plan.api_key is not None and plan.model is not None
        self._secret = plan.api_key.value
        self.model = plan.model
        try:
            assert_sdk_pin()
            from google import genai

            client = genai.Client(api_key=self._secret, vertexai=False)
            cm = client.aio.live.connect(
                model=plan.model,
                config=live_connect_config(self.tool_behavior),
            )
            session = await cm.__aenter__()
        except Exception as exc:
            message = self._safe(exc)
            self._secret = None
            self.model = None
            raise ProviderSessionError(message) from None
        self._client = client
        self._cm = cm
        return session

    async def _send_loop(self) -> None:
        try:
            while True:
                pcm = await self._uplink.get()
                if pcm is None:
                    return
                session = self._session
                if session is None:
                    return
                try:
                    await session.send_realtime_input(  # type: ignore[attr-defined]
                        audio={
                            "data": pcm,
                            "mime_type": f"audio/pcm;rate={INPUT_SAMPLE_RATE_HZ}",
                        }
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._fail(exc)
                    return
                self.sent_uplink_bytes += len(pcm)
                self._uplink.mark_idle_if_empty()
        except asyncio.CancelledError:
            raise

    async def _read_loop(self) -> None:
        try:
            while not self._shutdown_started:
                session = self._session
                if session is None:
                    break
                saw_message = False
                async for message in session.receive():  # type: ignore[attr-defined]
                    saw_message = True
                    stop = self._handle_message(message)
                    if stop or self._shutdown_started or self._events.overflowed:
                        await self._shutdown(reader=True)
                        return
                if not saw_message:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail(exc)
            return
        await self._shutdown(reader=True)

    def _handle_message(self, message: object) -> bool:
        self._note_resumption(message)
        if getattr(message, "go_away", None) is not None:
            self._events.push_control(VoiceEvent(kind="error", payload={"code": "go_away"}))
            return True
        cancellation = getattr(message, "tool_call_cancellation", None)
        if cancellation is not None:
            raw_ids = getattr(cancellation, "ids", None) or ()
            ids = tuple(str(item) for item in raw_ids)
            for call_id in ids:
                self._pending_tools.pop(call_id, None)
            self._events.push_control(
                VoiceEvent(
                    kind="error",
                    payload={"code": "tool_call_cancelled", "ids": ids},
                )
            )
        content = getattr(message, "server_content", None)
        interrupted = bool(content is not None and getattr(content, "interrupted", False))
        if not interrupted:
            for pcm, rate in _output_pcm(message):
                if self._emit_pcm(pcm, rate):
                    return True
        else:
            self._hold_generation()
            self._events.push_control(
                VoiceEvent(kind="interrupted", payload={"source": "server"})
            )
        tool_call = getattr(message, "tool_call", None)
        if tool_call is not None:
            for call in getattr(tool_call, "function_calls", None) or ():
                self._emit_tool_call(call)
        if content is not None:
            if getattr(content, "generation_complete", False):
                self._generation_complete = True
            status = _enum_value(getattr(content, "interaction_status", None))
            if status is not None:
                self._interaction_status = status
            if getattr(content, "turn_complete", False):
                transcript = getattr(content, "output_transcription", None)
                text = getattr(transcript, "text", None) if transcript is not None else None
                self._release_generation()
                self._events.push_control(
                    VoiceEvent(
                        kind="turn_complete",
                        payload={
                            "reason": _enum_value(
                                getattr(content, "turn_complete_reason", None)
                            ),
                            "generation_complete": self._generation_complete,
                            "interaction_status": self._interaction_status,
                            "transcript_present": bool(text),
                        },
                    )
                )
                self._generation_complete = False
        self.dropped_audio_events = self._events.dropped_audio
        self.flushed_audio_events = self._events.flushed_audio
        return self._events.overflowed

    def _hold_generation(self) -> None:
        """Stop emitting the generation that just ended or was cancelled."""

        self._suppress_audio = True
        self._resampler.end_turn()
        self._events.clear_audio()
        self.flushed_audio_events = self._events.flushed_audio

    def _release_generation(self) -> None:
        """Drop any fractional tail, then accept audio from the next turn."""

        self._resampler.end_turn()
        self._suppress_audio = False

    def _emit_pcm(self, pcm: bytes, rate: int | None) -> bool:
        """Resample one downlink chunk. Return True when the session must stop."""

        if self._suppress_audio:
            self.suppressed_audio_chunks += 1
            return False
        source = self.output_rate_hz if rate is None else rate
        if self._seen_audio and source != self._source_rate:
            self._events.push_control(
                VoiceEvent(kind="error", payload={"code": "sample_rate_changed"})
            )
            return True
        if not self._seen_audio and source != self._resampler.from_rate:
            self._resampler = StreamingResampler(source, AVIP_SAMPLE_RATE_HZ)
        self._source_rate = source
        self._seen_audio = True
        try:
            converted = self._resampler.push(pcm)
        except ValueError:
            self._events.push_control(
                VoiceEvent(kind="error", payload={"code": "invalid_audio"})
            )
            return True
        if converted:
            self._events.push_audio(converted)
            self.dropped_audio_events = self._events.dropped_audio
        return False

    def _emit_tool_call(self, call: object) -> None:
        call_id = getattr(call, "id", None)
        name = getattr(call, "name", None)
        if type(call_id) is not str or not call_id or type(name) is not str or not name:
            self._events.push_control(
                VoiceEvent(kind="error", payload={"code": "malformed_tool_call"})
            )
            return
        if len(self._pending_tools) >= _MAX_PENDING_TOOL_CALLS:
            self._events.push_control(
                VoiceEvent(kind="error", payload={"code": "tool_call_overflow"})
            )
            return
        self._pending_tools[call_id] = name
        self._events.push_control(
            VoiceEvent(
                kind="tool_call",
                payload={"id": call_id, "name": name, "args": _tool_args(call)},
            )
        )

    def _note_resumption(self, message: object) -> None:
        update = getattr(message, "session_resumption_update", None)
        if update is None:
            return
        if getattr(update, "resumable", False):
            self.resumption_updates += 1

    async def _fail(self, exc: BaseException) -> str:
        """Redact ``exc`` before shutdown clears the key. Return that text."""

        message = self._safe(exc)
        logger.warning("live provider session failed: %s", message)
        if self._shutdown_started:
            return message
        self._events.push_control(
            VoiceEvent(
                kind="error",
                payload={"code": "provider_error", "detail": message},
            )
        )
        await self._shutdown(reader=asyncio.current_task() is self._reader)
        return message

    async def _shutdown(self, *, reader: bool = False) -> None:
        if self._shutdown_started:
            if not reader:
                await self._shutdown_done.wait()
            return
        self._shutdown_started = True
        if self._state == "starting":
            self._shutdown_done.clear()
        self._state = "closing"
        self._uplink.close()
        await self._cancel_task(self._sender)
        self._sender = None
        if not reader:
            await self._cancel_task(self._reader)
            self._reader = None
        await self._close_session()
        self._events.finish()
        self._state = "closed"
        self._secret = None
        self._shutdown_done.set()

    async def _cancel_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None or task.done():
            return
        if task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        except Exception:
            return

    async def _close_session(self) -> None:
        cm = self._cm
        session = self._session
        self._cm = None
        self._session = None
        self._client = None
        try:
            if cm is not None:
                await cm.__aexit__(None, None, None)  # type: ignore[attr-defined]
            elif session is not None:
                close = getattr(session, "close", None)
                if callable(close):
                    await close()
        except Exception:
            return

    def _remember_redaction_key(self) -> None:
        try:
            plan = plan_smoke(self._env)
        except ModelUnconfigured:
            return
        if plan.api_key is not None:
            self._secret = plan.api_key.value
            self.model = plan.model

    def _fail_start(self) -> None:
        self._state = "closed"
        self._events.finish()
        self._shutdown_done.set()

    def _safe(self, exc: BaseException) -> str:
        return redact(f"{type(exc).__name__}: {exc}", self._secret)


def _tool_response_body(result: object) -> dict[str, Any]:
    if type(result) is not dict:
        raise TypeError("tool result must be a dict")
    if "output" in result or "error" in result:
        body: dict[str, Any] = result
    else:
        body = {"output": result}
    try:
        encoded = json.dumps(body, separators=(",", ":"))
    except TypeError as exc:
        raise TypeError("tool result must be JSON") from exc
    if len(encoded.encode()) > _MAX_TOOL_RESULT_BYTES:
        raise ValueError("tool result exceeds the size bound")
    return body


def _tool_args(call: object) -> dict[str, Any]:
    args = getattr(call, "args", None)
    if type(args) is dict:
        return args
    return {}


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    if raw is None:
        return None
    return str(raw)


def _rate_from_mime(mime: object) -> int | None:
    if type(mime) is not str:
        return None
    marker = "rate="
    index = mime.find(marker)
    if index < 0:
        return None
    digits = []
    for character in mime[index + len(marker) :]:
        if character.isdigit():
            digits.append(character)
        else:
            break
    if not digits:
        return None
    return int("".join(digits))


def _output_pcm(message: object) -> list[tuple[bytes, int | None]]:
    content = getattr(message, "server_content", None)
    turn = getattr(content, "model_turn", None) if content is not None else None
    parts = getattr(turn, "parts", None) if turn is not None else None
    found: list[tuple[bytes, int | None]] = []
    for part in parts or ():
        blob = getattr(part, "inline_data", None)
        data = getattr(blob, "data", None) if blob is not None else None
        if type(data) is bytes and data:
            mime = getattr(blob, "mime_type", None) if blob is not None else None
            found.append((data, _rate_from_mime(mime)))
    if found:
        return found
    data = getattr(message, "data", None)
    if type(data) is bytes and data:
        return [(data, None)]
    return []


async def run_duplex_roundtrip(
    env: Mapping[str, str], *, timeout_s: float = 45.0
) -> DuplexReport:
    """Send 16 kHz PCM, elicit one spoken turn, and measure time to first audio.

    Skips when ``GEMINI_API_KEY`` is unset. The spoken turn is text because
    this process has no microphone fixture. Uplink PCM is silence on the same
    session, sent before that turn.     The report stores byte counts and latency,
    not audio, the key, or a resumption handle. After the first audio event it
    sends ``CANCEL_SIGNAL`` so a credentialed run can see that the interrupt
    was accepted. Later audio from that generation is suppressed until
    ``turn_complete``.
    """

    try:
        plan = plan_smoke(env)
    except ModelUnconfigured:
        raise
    if plan.status == SmokeStatus.SKIPPED:
        return DuplexReport(status="skipped", reason=plan.reason)
    provider = GeminiLiveProvider(env=env, tool_behavior="NON_BLOCKING")
    downlink = 0
    first_ms: int | None = None
    saw_turn = False
    reason = "live duplex completed"
    status = "passed"
    cancel_accepted = False
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(timeout_s):
            await provider.start()
            silence = b"\x00\x00" * (INPUT_SAMPLE_RATE_HZ // 10)
            await provider.send_audio(silence)
            await provider._wait_uplink_idle()
            started = loop.time()
            await provider._submit_text(
                "Reply with one short spoken word: hello. Do not call any function."
            )
            async for event in provider.events():
                if event.kind == "audio" and type(event.payload) is bytes:
                    if first_ms is None:
                        first_ms = int((loop.time() - started) * 1000)
                    downlink += len(event.payload)
                    if not cancel_accepted:
                        await provider.cancel_response()
                        cancel_accepted = True
                elif event.kind == "interrupted":
                    continue
                elif event.kind == "turn_complete":
                    saw_turn = True
                    break
                elif event.kind == "error":
                    status = "failed"
                    reason = _event_reason(event)
                    break
                elif event.kind == "tool_call":
                    status = "failed"
                    reason = "unexpected tool call"
                    break
    except Exception as exc:
        status = "failed"
        reason = redact(f"{type(exc).__name__}: {exc}", provider._secret)
    finally:
        await provider.close()
    if status == "passed" and not cancel_accepted:
        status = "failed"
        reason = "cancel signal was not sent"
    elif status == "passed" and (not saw_turn or downlink <= 0):
        status = "failed"
        reason = "duplex turn ended without downlink audio"
    return DuplexReport(
        status=status,
        reason=reason,
        model=plan.model,
        uplink_bytes=provider.sent_uplink_bytes,
        downlink_bytes=downlink,
        first_audio_ms=first_ms,
        saw_turn_complete=saw_turn,
        cancel_signal=CANCEL_SIGNAL,
        cancel_accepted=cancel_accepted,
    )


async def run_tool_handshake(
    env: Mapping[str, str], *, timeout_s: float = 45.0
) -> ToolHandshakeReport:
    """Call ``avi_ack`` once under BLOCKING and once under NON_BLOCKING.

    Each attempt sets ``FunctionDeclaration.behavior`` explicitly, waits to see
    whether audio arrives before ``tool_result``, then sends
    ``send_tool_response``. NON_BLOCKING responses set the SDK scheduling field
    to ``WHEN_IDLE``. BLOCKING responses omit it. The report does not include
    tool arguments, audio, keys, or resumption handles.
    """

    try:
        plan = plan_smoke(env)
    except ModelUnconfigured:
        raise
    if plan.status == SmokeStatus.SKIPPED:
        return ToolHandshakeReport(status="skipped", reason=plan.reason)
    reports = []
    for behavior in ("NON_BLOCKING", "BLOCKING"):
        reports.append(await _one_tool_behavior(env, behavior, timeout_s))
    failed = [item for item in reports if item.status != "passed"]
    if failed:
        return ToolHandshakeReport(
            status="failed",
            reason=failed[0].reason,
            model=plan.model,
            behaviors=tuple(reports),
        )
    return ToolHandshakeReport(
        status="passed",
        reason="tool handshake completed for BLOCKING and NON_BLOCKING",
        model=plan.model,
        behaviors=tuple(reports),
    )


async def _one_tool_behavior(
    env: Mapping[str, str], behavior: str, timeout_s: float
) -> ToolBehaviorReport:
    scheduling = "WHEN_IDLE" if behavior == "NON_BLOCKING" else None
    provider = GeminiLiveProvider(env=env, tool_behavior=behavior)
    saw_tool = False
    audio_before = False
    audio_after = False
    saw_turn = False
    responded = False
    status = "passed"
    reason = "tool handshake completed"
    call_id = ""
    inbox: asyncio.Queue[VoiceEvent | None] = asyncio.Queue()

    async def _pump() -> None:
        async for event in provider.events():
            await inbox.put(event)
        await inbox.put(None)

    pump: asyncio.Task[None] | None = None
    try:
        async with asyncio.timeout(timeout_s):
            await provider.start()
            pump = asyncio.create_task(_pump(), name="avi-live-tool-smoke")
            await provider._submit_text(
                "Call the function avi_ack exactly once, with empty arguments. "
                "After the function result, say one short spoken word."
            )
            while not saw_tool:
                event = await inbox.get()
                if event is None:
                    status = "failed"
                    reason = "stream ended before the tool call"
                    break
                if event.kind == "tool_call" and isinstance(event.payload, dict):
                    saw_tool = True
                    raw_id = event.payload.get("id")
                    call_id = raw_id if type(raw_id) is str else ""
                elif event.kind == "audio":
                    audio_before = True
                elif event.kind == "error":
                    status = "failed"
                    reason = _event_reason(event)
                    break
                elif event.kind == "turn_complete":
                    status = "failed"
                    reason = "turn completed before the tool call"
                    break
            if status == "passed":
                try:
                    while True:
                        event = await asyncio.wait_for(inbox.get(), timeout=1.5)
                        if event is None:
                            status = "failed"
                            reason = "stream ended before the tool response"
                            break
                        if event.kind == "audio":
                            audio_before = True
                        elif event.kind == "turn_complete":
                            saw_turn = True
                            break
                        elif event.kind == "error":
                            status = "failed"
                            reason = _event_reason(event)
                            break
                except TimeoutError:
                    pass
            if status == "passed" and saw_tool:
                if not call_id:
                    status = "failed"
                    reason = "tool call had no id"
                else:
                    await provider.tool_result(call_id, {"ok": True})
                    responded = True
                    if not saw_turn:
                        while True:
                            event = await inbox.get()
                            if event is None:
                                break
                            if event.kind == "audio":
                                audio_after = True
                            elif event.kind == "turn_complete":
                                saw_turn = True
                                break
                            elif event.kind == "error":
                                status = "failed"
                                reason = _event_reason(event)
                                break
                    else:
                        try:
                            event = await asyncio.wait_for(inbox.get(), timeout=2.0)
                        except TimeoutError:
                            event = None
                        if event is not None and event.kind == "audio":
                            audio_after = True
                        elif event is not None and event.kind == "error":
                            status = "failed"
                            reason = _event_reason(event)
    except Exception as exc:
        status = "failed"
        reason = redact(f"{type(exc).__name__}: {exc}", provider._secret)
    finally:
        await provider.close()
        if pump is not None:
            try:
                await pump
            except Exception:
                pass
    if status == "passed" and not saw_tool:
        status = "failed"
        reason = "no tool call"
    if status == "passed" and not responded and not saw_turn:
        status = "failed"
        reason = "tool response was not sent"
    if status == "passed" and not saw_turn:
        status = "failed"
        reason = "no turn_complete after the tool response"
    return ToolBehaviorReport(
        behavior=behavior,
        status=status,
        reason=reason,
        scheduling=scheduling,
        saw_tool_call=saw_tool,
        audio_before_tool_response=audio_before,
        audio_after_tool_response=audio_after,
        saw_turn_complete=saw_turn,
    )


def _event_reason(event: VoiceEvent) -> str:
    payload = event.payload if isinstance(event.payload, dict) else {}
    code = payload.get("code", event.kind)
    detail = payload.get("detail")
    if type(detail) is str and detail:
        return f"{code}: {detail}"
    return str(code)
