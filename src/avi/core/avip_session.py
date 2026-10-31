"""Connection-scoped AVIP session.

A session id exists only while its WebSocket is open. Disconnect discards it.
This path does not construct an agent, submit a run, or retain anything that
a later connection could resume.

After ``session.ready``, text frames stay on the JSON control plane. One binary
message is one media frame, passed through ``unpack_frame``. The same driver
handles every device label.
"""
import asyncio
import secrets
from typing import NoReturn, Protocol

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
    next_sequence,
    pack_frame,
    unpack_frame,
)
from avi.transports.base import TransportDisconnected, TransportFrameError


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


class AvipSession:
    def __init__(self, transport: AvipTransport, limits: SessionLimits,
                 available: Capabilities):
        self.transport = transport
        self.limits = limits
        self.available = available
        self.session_id: str | None = None
        self.negotiated: Capabilities | None = None
        self.delivery: MediaDelivery | None = None
        self._downlink_sequence = 0
        self._downlink_timestamp = 0

    async def _send(self, payload: dict) -> None:
        try:
            async with asyncio.timeout(self.limits.send_timeout):
                await self.transport.send(payload)
        except TimeoutError:
            raise _SessionEnd(1013, "Send timeout") from None

    async def _send_bytes(self, payload: bytes) -> None:
        try:
            async with asyncio.timeout(self.limits.send_timeout):
                await self.transport.send_bytes(payload)
        except TimeoutError:
            raise _SessionEnd(1013, "Send timeout") from None

    async def _reject(self, error: ProtocolError, code: int, reason: str) -> NoReturn:
        await self._send(error_envelope(error))
        raise _SessionEnd(code, reason)

    async def _receive(self) -> tuple[str, dict | bytes]:
        try:
            async with asyncio.timeout(self.limits.idle_timeout):
                return await self.transport.receive_message()
        except TimeoutError:
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
        await self._send(ready)
        self.session_id = ready["session_id"]
        self.negotiated = Capabilities(**ready["capabilities"])
        self.delivery = MediaDelivery()

    async def _on_media(self, data: bytes) -> None:
        """Queue one uplink frame and, when output was negotiated, mirror it.

        The mirror is not a voice provider. It copies the PCM payload into a
        new downlink frame. Downlink sequence and timestamp belong to the
        gateway clock: sequence starts at 0, and the timestamp starts at 0 and
        advances by the payload duration at 16 kHz. Those clocks are not the
        client's clocks.

        Accepted uplink frames stay queued. Nothing here consumes them, so the
        depth cap is reachable. ``queue_full`` is an error envelope and leaves
        the socket open; the refused frame keeps its sequence. Any other frame
        error is fatal for this connection.
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
                await self._send(error_envelope(exc))
                return
            await self._reject(exc, 1002, "Protocol error")
        if not negotiated.audio_output:
            return
        echo = MediaFrame(
            Direction.AUDIO_DOWNLINK,
            self._downlink_sequence,
            self._downlink_timestamp,
            frame.payload,
        )
        await self._send_bytes(pack_frame(echo))
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

    async def run(self) -> None:
        ending = None
        try:
            try:
                await self._handshake()
                await self._until_disconnect()
            except _SessionEnd as exc:
                ending = exc
            except TransportDisconnected:
                pass
        finally:
            self.session_id = None
            self.negotiated = None
            self.delivery = None
            if ending is not None:
                try:
                    async with asyncio.timeout(self.limits.send_timeout):
                        await self.transport.close(ending.code, ending.reason)
                except (TimeoutError, TransportDisconnected):
                    pass
