"""Connection-scoped AVIP handshake.

A session id exists only while its WebSocket is open. Disconnect discards it.
This path does not construct an agent, submit a run, or retain anything that
a later connection could resume.
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
from avi.transports.base import TransportDisconnected, TransportFrameError


def allocate_session_id() -> str:
    """Return a new opaque id in the AVIP identifier grammar.

    The id is not derived from the client and cannot be resolved after the
    connection closes.
    """
    return "s" + secrets.token_hex(16)


class AvipTransport(Protocol):
    """Text JSON socket used by the handshake. Binary media is not this port."""

    async def receive_json_object(self) -> dict: ...
    async def send(self, event: dict) -> None: ...
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

    async def _send(self, payload: dict) -> None:
        try:
            async with asyncio.timeout(self.limits.send_timeout):
                await self.transport.send(payload)
        except TimeoutError:
            raise _SessionEnd(1013, "Send timeout") from None

    async def _reject(self, error: ProtocolError, code: int, reason: str) -> NoReturn:
        await self._send(error_envelope(error))
        raise _SessionEnd(code, reason)

    async def _receive(self) -> dict:
        try:
            async with asyncio.timeout(self.limits.idle_timeout):
                return await self.transport.receive_json_object()
        except TimeoutError:
            raise _SessionEnd(1001, "Idle timeout") from None
        except TransportFrameError as exc:
            # Binary is not a media path. Drop the frame and close.
            if exc.binary:
                await self._reject(ProtocolError("invalid_message"), 1003, "Unsupported data")
            else:
                await self._reject(ProtocolError("invalid_message"), 1002, "Protocol error")

    async def _handshake(self) -> None:
        message = await self._receive()
        try:
            start = parse_session_start(message)
        except ProtocolError as exc:
            await self._reject(exc, 1002, "Protocol error")
        ready = session_ready(start, allocate_session_id(), self.available)
        await self._send(ready)
        self.session_id = ready["session_id"]

    async def _until_disconnect(self) -> None:
        while self.session_id is not None:
            message = await self._receive()
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
            if ending is not None:
                try:
                    async with asyncio.timeout(self.limits.send_timeout):
                        await self.transport.close(ending.code, ending.reason)
                except (TimeoutError, TransportDisconnected):
                    pass
