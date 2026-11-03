"""Connection-scoped AVIP session.

A session id exists only while its WebSocket is open. Disconnect discards it.
This path does not construct an agent, submit a run, or retain anything that
a later connection could resume.

The connection's VoiceSession moves connecting → active when ``session.ready``
has been sent, and active or connecting → closing → closed when the socket
ends. Peer disconnect and idle timeout record ``disconnected``. Protocol and
send failures record ``failed``. This driver does not enter ``reconnecting``
and does not create a VoiceResponse or an AgentRun.

After ``session.ready``, text frames stay on the JSON control plane. One binary
message is one media frame, passed through ``unpack_frame``. Accepted uplink
frames are queued. A mock sink takes them when this session has no voice
provider. When a ``VoiceProvider`` is attached and audio was negotiated, the
same frames are forwarded to that provider and its 16 kHz audio events come
back as downlink frames. Provider control events stay in this process. They
are not written as new AVIP message types. The same driver handles every
device label.
"""
import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import NoReturn, Protocol

from avi.telemetry import VoiceTelemetry

from avi.core.downlink import DownlinkPacer
from avi.core.events import VoiceEvent
from avi.core.lifecycle import SessionCloseReason, VoiceSession, VoiceSessionState
from avi.core.session import SessionLimits
from avi.protocols.avip import (
    Capabilities,
    ProtocolError,
    error_envelope,
    is_session_start,
    parse_session_start,
    session_ready,
)
from avi.protocols.media import (
    SAMPLE_RATE_HZ,
    SAMPLE_WIDTH_BYTES,
    Direction,
    MediaDelivery,
    MediaFrame,
    MockMediaSink,
    next_sequence,
    pack_frame,
    unpack_frame,
)
from avi.providers.base import VoiceProvider
from avi.transports.base import TransportDisconnected, TransportFrameError

logger = logging.getLogger(__name__)


def allocate_session_id() -> str:
    """Return a new opaque id in the AVIP identifier grammar.

    The id is not derived from the client and cannot be resolved after the
    connection closes.
    """
    return "s" + secrets.token_hex(16)


def _duration_us(payload: bytes) -> int:
    """Return how long ``payload`` lasts on the M0 16 kHz mono clock."""
    samples = len(payload) // SAMPLE_WIDTH_BYTES
    return samples * 1_000_000 // SAMPLE_RATE_HZ


class AvipTransport(Protocol):
    """Socket used by one AVIP connection. Text is JSON; binary is one frame."""

    async def receive_message(self) -> tuple[str, dict | bytes]: ...
    async def send(self, event: dict) -> None: ...
    async def send_bytes(self, payload: bytes) -> None: ...
    async def close(self, code: int, reason: str) -> None: ...


class _SessionEnd(Exception):
    def __init__(self, code: int, reason: str):
        self.code = code
        self.reason = reason


# Local closes that mean the handshake or the socket failed. Idle (1001) and a
# peer drop are disconnects, not failures. Any other local close stays a
# failure so a new code cannot be recorded as a clean disconnect.
_FAILURE_CLOSE_CODES = frozenset({1002, 1003, 1013})
_PROVIDER_CLOSE_CODE = 1011
_PROVIDER_CLOSE_REASON = "Voice provider unavailable"
_PROVIDER_TASK = "avip-voice-provider"
_RECEIVE_TASK = "avip-receive"


def _provider_error_code(payload: object) -> str:
    """Return a loggable provider code. Payloads are not copied."""

    if not isinstance(payload, dict):
        return "provider_error"
    code = payload.get("code")
    if type(code) is not str or not code.isidentifier() or len(code) > 41:
        return "provider_error"
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_"
    if code[0] not in "abcdefghijklmnopqrstuvwxyz":
        return "provider_error"
    if any(character not in allowed for character in code):
        return "provider_error"
    return code


def _ending_from(
    done: set[asyncio.Task[None]], provider_task: asyncio.Task[None] | None,
) -> _SessionEnd | None:
    """Pick the socket outcome from the first finished session tasks.

    A provider stream that ends on its own closes the AVIP socket. That is
    not session renewal. Disconnect of the peer stays a disconnect.
    """

    ending: _SessionEnd | None = None
    provider_ended = False
    for task in done:
        if task.cancelled():
            continue
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            continue
        if isinstance(exc, _SessionEnd):
            return exc
        if exc is None and task is provider_task:
            provider_ended = True
            continue
        if isinstance(exc, TransportDisconnected) or exc is None:
            continue
        logger.warning("voice session task failed: %s", type(exc).__name__)
        ending = _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON)
    if provider_ended:
        return _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON)
    return ending


def _session_close_reason(ending: _SessionEnd | None) -> SessionCloseReason:
    if ending is not None and ending.code in _FAILURE_CLOSE_CODES:
        return SessionCloseReason.FAILED
    if ending is None or ending.code == 1001:
        return SessionCloseReason.DISCONNECTED
    return SessionCloseReason.FAILED


class AvipSession:
    def __init__(self, transport: AvipTransport, limits: SessionLimits,
                 available: Capabilities, *,
                 clock: Callable[[], float] | None = None,
                 wall_clock: Callable[[], float] | None = None,
                 telemetry_path: str = "mock-mirror",
                 telemetry_scenario: str = "gateway",
                 provider: VoiceProvider | None = None,
                 elicit: str | None = None,
                 monotonic: Callable[[], float] | None = None,
                 sleeper: Callable[[float], Awaitable[None]] | None = None):
        self.transport = transport
        self.limits = limits
        self.available = available
        self.provider = provider
        self.elicit = elicit
        self._monotonic = time.monotonic if monotonic is None else monotonic
        self._sleeper = asyncio.sleep if sleeper is None else sleeper
        self.session_id: str | None = None
        self.negotiated: Capabilities | None = None
        self.delivery: MediaDelivery | None = None
        self.sink: MockMediaSink | None = None
        self._provider_open = False
        self._provider_task: asyncio.Task[None] | None = None
        self._pacer: DownlinkPacer | None = None
        self._send_lock = asyncio.Lock()
        self.voice_session = VoiceSession()
        # Session clock is monotonic milliseconds in this process. It is not
        # the client's page clock and not the AVIP media timestamp.
        # Defaults describe the mock sink. JEV-23 must pass
        # telemetry_path="provider" when a real provider is attached, or
        # model latency is reported as mock-mirror.
        self.telemetry = VoiceTelemetry(
            client="gateway",
            path=telemetry_path,
            scenario=telemetry_scenario,
            now=clock,
            wall_now=wall_clock,
        )
        self.telemetry_report: dict | None = None
        self._downlink_sequence = 0
        self._downlink_timestamp = 0

    async def _send(self, payload: dict) -> None:
        try:
            async with self._send_lock:
                async with asyncio.timeout(self.limits.send_timeout):
                    await self.transport.send(payload)
        except TimeoutError:
            self.telemetry.note_timeout("send")
            raise _SessionEnd(1013, "Send timeout") from None

    async def _send_bytes(self, payload: bytes) -> None:
        try:
            async with self._send_lock:
                async with asyncio.timeout(self.limits.send_timeout):
                    await self.transport.send_bytes(payload)
        except TimeoutError:
            self.telemetry.note_timeout("send")
            raise _SessionEnd(1013, "Send timeout") from None

    async def _reject(self, error: ProtocolError, code: int, reason: str) -> NoReturn:
        await self._send(error_envelope(error))
        raise _SessionEnd(code, reason)

    async def _receive(self) -> tuple[str, dict | bytes]:
        try:
            async with asyncio.timeout(self.limits.idle_timeout):
                return await self.transport.receive_message()
        except TimeoutError:
            self.telemetry.note_timeout("idle")
            raise _SessionEnd(1001, "Idle timeout") from None
        except TransportFrameError as exc:
            if exc.binary:
                await self._reject(ProtocolError("invalid_message"), 1003, "Unsupported data")
            else:
                await self._reject(ProtocolError("invalid_message"), 1002, "Protocol error")

    async def _handshake(self) -> None:
        kind, message = await self._receive()
        if kind != "text":
            # Bytes before a session are not a media frame. Do not decode them.
            await self._reject(ProtocolError("invalid_message"), 1003, "Unsupported data")
        try:
            start = parse_session_start(message)
        except ProtocolError as exc:
            await self._reject(exc, 1002, "Protocol error")
        ready = session_ready(start, allocate_session_id(), self.available)
        self.negotiated = Capabilities(**ready["capabilities"])
        self.delivery = MediaDelivery()
        if self._should_open_provider():
            await self._open_provider()
        await self._send(ready)
        self.session_id = ready["session_id"]
        self.voice_session.activate(
            ready["session_id"],
            request_id=start.request_id,
            device_id=start.device_id,
        )
        self.telemetry.set_session_id(ready["session_id"])
        self.telemetry.mark("session-start")
        if self._provider_open:
            self._pacer = DownlinkPacer(
                self._send_downlink_frame,
                self._send,
                monotonic=self._monotonic,
                sleeper=self._sleeper,
            )
            self._provider_task = asyncio.create_task(
                self._pump_provider(), name=_PROVIDER_TASK,
            )
            await self._elicit()
        elif self.negotiated.audio_input:
            self.sink = MockMediaSink(self.delivery)
            self.sink.start()

    async def _on_media(self, data: bytes) -> None:
        """Queue one uplink frame and, when output was negotiated, mirror it.

        The mirror is not a voice provider. It copies the PCM payload into a
        new downlink frame. Downlink sequence and timestamp belong to the
        gateway clock: sequence starts at 0, and the timestamp starts at 0 and
        advances by the payload duration at 16 kHz. Those clocks are not the
        client's clocks.

        Accepted uplink frames are offered to the session mock sink, unless a
        voice provider is open. The provider takes the frame on this turn and
        does not mirror it. When the sink is willing, ``reclaim`` takes them
        before this method reads another frame, on this turn, with no
        scheduler yield. A paused or metered sink leaves the frame queued.
        The mirror does not take it.
        ``queue_full`` is an error envelope and leaves the socket open; the
        refused frame keeps its sequence. Any other frame error is fatal for
        this connection.
        """
        negotiated = self.negotiated
        delivery = self.delivery
        if negotiated is None or delivery is None or not negotiated.audio_input:
            await self._reject(ProtocolError("invalid_message"), 1003, "Unsupported data")
        try:
            frame = unpack_frame(data)
        except ProtocolError as exc:
            await self._reject(exc, 1002, "Protocol error")
        if frame.direction is not Direction.AUDIO_UPLINK:
            await self._reject(
                ProtocolError("invalid_frame", "direction"), 1002, "Protocol error"
            )
        try:
            delivery.submit(frame)
        except ProtocolError as exc:
            if exc.code == "queue_full":
                self.telemetry.note_queue_full()
                await self._send(error_envelope(exc))
                return
            await self._reject(exc, 1002, "Protocol error")
        observed = self.telemetry.now()
        self.telemetry.note_buffer_depth(delivery.depth(Direction.AUDIO_UPLINK))
        self.telemetry.note_uplink(observed)
        self.telemetry.mark("first-uplink", observed)
        self.telemetry.add_counter("uplinkFrames")
        if self._provider_open:
            taken = delivery.take(Direction.AUDIO_UPLINK)
            if taken is not None:
                await self._forward_uplink(taken.payload)
            return
        if self.sink is not None:
            self.sink.reclaim()
        if negotiated.audio_output:
            echo = MediaFrame(
                Direction.AUDIO_DOWNLINK,
                self._downlink_sequence,
                self._downlink_timestamp,
                frame.payload,
            )
            await self._send_bytes(pack_frame(echo))
            mirrored = self.telemetry.now()
            self.telemetry.note_downlink(mirrored)
            self.telemetry.mark("first-downlink", mirrored)
            self.telemetry.add_counter("downlinkFrames")
            self._downlink_sequence = next_sequence(self._downlink_sequence)
            self._downlink_timestamp += _duration_us(frame.payload)

    async def _until_disconnect(self) -> None:
        while self.session_id is not None:
            kind, message = await self._receive()
            if kind == "binary":
                if not isinstance(message, bytes):
                    await self._reject(ProtocolError("invalid_message"), 1003, "Unsupported data")
                await self._on_media(message)
                continue
            if is_session_start(message):
                await self._reject(
                    ProtocolError("session_already_started"), 1002, "Protocol error"
                )
            await self._send(error_envelope(ProtocolError("unsupported_message_type", "type")))

    def _should_open_provider(self) -> bool:
        negotiated = self.negotiated
        return (
            self.provider is not None
            and negotiated is not None
            and (negotiated.audio_input or negotiated.audio_output)
        )

    async def _open_provider(self) -> None:
        """Start the provider before ``session.ready``.

        A failed start does not advertise a live audio session. The exception
        text is not logged: it can carry request details even after redaction.
        """

        provider = self.provider
        if provider is None:
            return
        try:
            async with asyncio.timeout(self.limits.operation_timeout):
                await provider.start()
        except TimeoutError:
            logger.warning("voice provider start timed out")
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("voice provider failed to start")
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON) from None
        self._provider_open = True

    async def _forward_uplink(self, payload: bytes) -> None:
        provider = self.provider
        if provider is None:
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON)
        try:
            await provider.send_audio(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("voice provider rejected uplink")
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON) from None

    async def _elicit(self) -> None:
        """One server-side text turn. Not an AVIP message and not logged."""

        text = self.elicit
        provider = self.provider
        if not text or provider is None:
            return
        submit = getattr(provider, "_submit_text", None)
        if submit is None:
            return
        try:
            await submit(text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("voice provider prompt failed")
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON) from None

    async def _pump_provider(self) -> None:
        """Pace provider audio and map control events onto AVIP.

        ``audio`` becomes nominal 20 ms downlink frames, held to a 160 ms
        horizon. ``interrupted`` is ``response.interrupted`` for that
        generation and drops only unsent frames. ``turn_complete`` stays
        inside the gateway. ``error`` ends the socket. GoAway is that path
        and does not open a replacement session.
        """

        provider = self.provider
        pacer = self._pacer
        if provider is None or pacer is None:
            return
        try:
            await pacer.run(provider.events(), self._on_provider_control)
        except asyncio.CancelledError:
            raise
        except _SessionEnd:
            raise
        except ValueError:
            logger.warning("voice provider audio was not a pcm frame")
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON) from None
        except Exception:
            logger.warning("voice provider bridge failed")
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON) from None

    async def _on_provider_control(self, event: VoiceEvent) -> None:
        if event.kind == "tool_call":
            await self._ack_tool(event.payload)
            return
        if event.kind == "error":
            logger.warning(
                "voice provider ended the session: %s",
                _provider_error_code(event.payload),
            )
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON)
        if event.kind == "turn_complete":
            return
        if event.kind == "interrupted":
            return
        logger.warning("voice provider event ignored")

    async def _ack_tool(self, payload: object) -> None:
        if not isinstance(payload, dict):
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON)
        call_id = payload.get("id")
        if type(call_id) is not str or not call_id or len(call_id) > 256:
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON)
        provider = self.provider
        if provider is None:
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON)
        try:
            await provider.tool_result(call_id, {"output": {}})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("voice provider tool result failed")
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON) from None

    async def _send_downlink_frame(self, chunk: bytes) -> None:
        negotiated = self.negotiated
        if negotiated is None or not negotiated.audio_output:
            return
        if len(chunk) == 0 or len(chunk) % SAMPLE_WIDTH_BYTES != 0:
            logger.warning("voice provider audio was not a pcm frame")
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON)
        try:
            packed = pack_frame(MediaFrame(
                Direction.AUDIO_DOWNLINK,
                self._downlink_sequence,
                self._downlink_timestamp,
                chunk,
            ))
        except ProtocolError:
            logger.warning("voice provider downlink frame was rejected")
            raise _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON) from None
        await self._send_bytes(packed)
        observed = self.telemetry.now()
        self.telemetry.note_downlink(observed)
        self.telemetry.mark("first-downlink", observed)
        self.telemetry.add_counter("downlinkFrames")
        self._downlink_sequence = next_sequence(self._downlink_sequence)
        self._downlink_timestamp += _duration_us(chunk)

    async def _wait_until_end(self, receive_task: asyncio.Task[None]) -> _SessionEnd | None:
        watched = {receive_task}
        provider_task = self._provider_task
        if provider_task is not None:
            watched.add(provider_task)
        done, _pending = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        ending = _ending_from(done, provider_task)
        for task in watched:
            if not task.done():
                task.cancel()
        for task in watched:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except _SessionEnd as exc:
                if ending is None:
                    ending = exc
            except TransportDisconnected:
                pass
            except Exception as exc:
                logger.warning("voice session task failed: %s", type(exc).__name__)
                if ending is None:
                    ending = _SessionEnd(_PROVIDER_CLOSE_CODE, _PROVIDER_CLOSE_REASON)
        return ending

    async def _stop_voice(self) -> None:
        """Cancel the bridge and close the provider, then the mock sink."""

        task = self._provider_task
        self._provider_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                async with asyncio.timeout(self.limits.cleanup_timeout):
                    await task
            except TimeoutError:
                logger.warning("voice provider bridge stop timed out")
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning("voice provider bridge stopped")
        provider = self.provider
        self.provider = None
        try:
            if provider is not None:
                try:
                    async with asyncio.timeout(self.limits.cleanup_timeout):
                        await provider.close()
                except TimeoutError:
                    logger.warning("voice provider close timed out")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("voice provider close failed")
        finally:
            sink = self.sink
            self.sink = None
            try:
                if sink is not None:
                    try:
                        async with asyncio.timeout(self.limits.cleanup_timeout):
                            await sink.stop()
                    except Exception:
                        logger.exception("Mock media sink cleanup failed")
            finally:
                if self.delivery is not None:
                    self.delivery.clear()
                self.delivery = None
                self.session_id = None
                self.negotiated = None
                self._provider_open = False

    def _finish_telemetry(self) -> None:
        """Snapshot counters once. The log line has no PCM and no credentials."""
        sink = self.sink
        if sink is not None:
            self.telemetry.note_sink(sink.high_water, sink.consumed)
        if self.session_id is not None:
            self.telemetry.set_session_id(self.session_id)
        self.telemetry_report = self.telemetry.report()
        logger.info(
            "voice_telemetry %s",
            json.dumps(self.telemetry_report, separators=(",", ":"), sort_keys=True),
        )

    def _end_voice_session(self, ending: _SessionEnd | None) -> None:
        """Record the socket outcome on the voice session, if it is still open."""
        if self.voice_session.state is VoiceSessionState.CLOSED:
            return
        self.voice_session.cleanup(_session_close_reason(ending))

    async def run(self) -> None:
        ending = None
        receive_task: asyncio.Task[None] | None = None
        try:
            try:
                await self._handshake()
                receive_task = asyncio.create_task(
                    self._until_disconnect(), name=_RECEIVE_TASK,
                )
                ending = await self._wait_until_end(receive_task)
            except _SessionEnd as exc:
                ending = exc
            except TransportDisconnected:
                pass
        finally:
            if receive_task is not None and not receive_task.done():
                receive_task.cancel()
                try:
                    await receive_task
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                self._finish_telemetry()
            except Exception:
                logger.exception("voice telemetry report failed")
            try:
                self._end_voice_session(ending)
            finally:
                try:
                    await self._stop_voice()
                finally:
                    if ending is not None:
                        try:
                            async with asyncio.timeout(self.limits.send_timeout):
                                await self.transport.close(ending.code, ending.reason)
                        except (TimeoutError, TransportDisconnected):
                            pass
