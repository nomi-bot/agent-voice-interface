"""Bounded control-session scheduling, independent of the WebSocket framework."""
import asyncio
import logging
from dataclasses import dataclass
from math import isfinite

from avi.agents.base import AgentAdapter
from avi.core.orchestrator import VoiceOrchestrator
from avi.transports.base import TransportAdapter, TransportDisconnected

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionLimits:
    idle_timeout: float = 300
    operation_timeout: float = 30
    send_timeout: float = 5
    cleanup_timeout: float = 5
    pending_operations: int = 16
    pending_replies: int = 16

    def __post_init__(self):
        for value in (self.pending_operations, self.pending_replies):
            if not isinstance(value, int) or value <= 0:
                raise ValueError("Queue sizes must be positive integers")
        for value in (self.idle_timeout, self.operation_timeout,
                      self.send_timeout, self.cleanup_timeout):
            if not isfinite(value) or value <= 0:
                raise ValueError("Timeouts must be positive and finite")


class _SessionEnd(Exception):
    def __init__(self, code: int, reason: str):
        self.code = code
        self.reason = reason


class ControlSession:
    def __init__(self, agent: AgentAdapter, transport: TransportAdapter,
                 limits: SessionLimits = SessionLimits()):
        self.agent = agent
        self.transport = transport
        self.limits = limits
        self.control = VoiceOrchestrator(agent)
        self.operations: asyncio.Queue[dict] = asyncio.Queue(limits.pending_operations)
        self.replies: asyncio.Queue[dict] = asyncio.Queue(limits.pending_replies)

    def _reply(self, reply: dict) -> None:
        try:
            self.replies.put_nowait(reply)
        except asyncio.QueueFull:
            raise _SessionEnd(1013, "Reply queue full") from None

    async def _send(self, reply: dict) -> None:
        try:
            async with asyncio.timeout(self.limits.send_timeout):
                await self.transport.send(reply)
        except TimeoutError:
            raise _SessionEnd(1013, "Send timeout") from None

    async def _receive(self) -> None:
        while True:
            try:
                async with asyncio.timeout(self.limits.idle_timeout):
                    message = await self.transport.receive()
                kind = self.control.message_type(message)
            except TimeoutError:
                raise _SessionEnd(1001, "Idle timeout") from None
            except (ValueError, KeyError):
                self._reply({"type": "error", "message": "Invalid message or unknown run"})
                continue
            if kind in {"ping", "stop_playback"}:
                # These operations never touch an adapter or await external I/O.
                self._reply(await self.control.handle(message))
            else:
                try:
                    self.operations.put_nowait(message)
                except asyncio.QueueFull:
                    self._reply({"type": "error", "message": "Too many pending operations"})

    async def _work(self) -> None:
        while True:
            message = await self.operations.get()
            try:
                async with asyncio.timeout(self.limits.operation_timeout):
                    reply = await self.control.handle(message)
            except TimeoutError:
                reply = {"type": "error", "message": "Agent operation timed out"}
            except (ValueError, KeyError):
                reply = {"type": "error", "message": "Invalid message or unknown run"}
            except Exception:
                logger.exception("Agent operation failed")
                reply = {"type": "error", "message": "Agent operation failed"}
            self._reply(reply)

    async def _write(self) -> None:
        while True:
            await self._send(await self.replies.get())

    async def run(self, ready: dict) -> None:
        ending = None
        try:
            try:
                await self._send(ready)
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self._receive())
                    tasks.create_task(self._work())
                    tasks.create_task(self._write())
            except* _SessionEnd as errors:
                ending = errors.exceptions[0]
            except* TransportDisconnected:
                pass
        finally:
            try:
                async with asyncio.timeout(self.limits.cleanup_timeout):
                    await self.agent.close()
            except Exception:
                logger.exception("Agent cleanup failed")
            finally:
                if ending is not None:
                    try:
                        async with asyncio.timeout(self.limits.send_timeout):
                            await self.transport.close(ending.code, ending.reason)
                    except (TimeoutError, TransportDisconnected):
                        pass
