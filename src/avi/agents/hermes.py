"""Hermes adapter for the durable-run service.

The caller owns authorization, idempotent admission and the last confirmed
observation. The adapter performs at most one transport operation per call.
"""

import asyncio
import math
import re
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from avi.core.lifecycle import AGENT_RUN_TRANSITIONS, AgentRunState


# Run results share the lifecycle's 4096-character limit.
_MAX_RESULT = 4096
_MAX_PROGRESS = 512
_OPAQUE_ID = re.compile(r"[!-~]{1,255}")
_ACTIVE_ORDER = {
    AgentRunState.SUBMITTED: 0,
    AgentRunState.QUEUED: 1,
    AgentRunState.RUNNING: 2,
}


class HermesErrorCode(StrEnum):
    APPROVAL_DENIED = "approval_denied"
    CREDENTIALS_REJECTED = "credentials_rejected"
    UNSUPPORTED = "unsupported"
    SUBMIT_UNCERTAIN = "submit_uncertain"
    CANCEL_UNCERTAIN = "cancel_uncertain"
    REMOTE_OUTCOME_UNKNOWN = "remote_outcome_unknown"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"
    INVALID_REQUEST = "invalid_request"
    REQUEST_REJECTED = "request_rejected"
    NO_RUN_EVIDENCE = "no_run_evidence"
    CLOSED = "closed"


_ERROR_OUTCOMES = {
    HermesErrorCode.APPROVAL_DENIED: "denied",
    HermesErrorCode.CREDENTIALS_REJECTED: "denied",
    HermesErrorCode.UNSUPPORTED: "unsupported",
    HermesErrorCode.SUBMIT_UNCERTAIN: "uncertain",
    HermesErrorCode.CANCEL_UNCERTAIN: "uncertain",
    HermesErrorCode.REMOTE_OUTCOME_UNKNOWN: "uncertain",
}


class HermesError(Exception):
    """Bounded error for callers. Upstream bodies and handles stay private."""

    def __init__(self, code: HermesErrorCode, *, correlation_id: str | None = None):
        self.code = HermesErrorCode(code)
        self.outcome = _ERROR_OUTCOMES.get(self.code, "failed")
        self.correlation_id = correlation_id
        super().__init__(self.code.value)


@dataclass(frozen=True)
class HermesCapabilities:
    status: bool = False
    progress: bool = False
    cancellation: bool = False
    correlation_lookup: bool = False


@dataclass(frozen=True)
class HermesExecutionContext:
    """Principal provided by the authenticated and authorized run service."""

    principal_id: str


@dataclass(frozen=True)
class HermesTask:
    kind: str
    arguments: Mapping[str, object] = field(repr=False)


@dataclass(frozen=True)
class HermesRun:
    handle: str = field(repr=False)
    state: AgentRunState
    result: str | None = field(default=None, repr=False)
    progress: str | None = field(default=None, repr=False)


class HermesTransport(Protocol):
    """Transport-owned normalization of upstream operations and evidence.

    Each method makes a bounded, cancellation-cooperative operation. A
    transport can raise a typed rejection only when it has evidence of it.
    Close releases local resources and leaves upstream runs alone.
    """

    capabilities: HermesCapabilities

    async def submit(
        self, task: HermesTask, context: HermesExecutionContext, correlation_id: str,
    ) -> HermesRun: ...

    async def status(self, handle: str, context: HermesExecutionContext) -> HermesRun: ...

    async def lookup(
        self, correlation_id: str, context: HermesExecutionContext,
    ) -> HermesRun | None: ...

    async def cancel(
        self, handle: str, context: HermesExecutionContext,
    ) -> HermesRun | None: ...

    async def close(self) -> None: ...


class HermesAgentAdapter:
    """A bounded Hermes service port with opaque handles and typed outcomes."""

    def __init__(
        self, transport: HermesTransport, *, timeout: float = 5, max_in_flight: int = 4,
    ):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if not isinstance(max_in_flight, int) or max_in_flight < 1:
            raise ValueError("max_in_flight must be a positive integer")
        self._transport = transport
        self.capabilities = transport.capabilities
        self._timeout = timeout
        self._slots = asyncio.Semaphore(max_in_flight)
        self._active: set[asyncio.Task] = set()
        self._close_task: asyncio.Task[None] | None = None

    async def submit(
        self, task: HermesTask, context: HermesExecutionContext, *, correlation_id: str,
    ) -> HermesRun:
        """Dispatch an intent the caller has already reserved durably."""
        _require_id(correlation_id)
        if not context.principal_id:
            raise HermesError(HermesErrorCode.CREDENTIALS_REJECTED)
        code = HermesErrorCode.SUBMIT_UNCERTAIN
        report = await self._request(
            lambda: self._transport.submit(task, context, correlation_id),
            uncertain=code,
            correlation_id=correlation_id,
        )
        return self._observation(report, error_code=code, correlation_id=correlation_id)

    async def status(
        self, handle: str, context: HermesExecutionContext, *, previous: HermesRun | None = None,
    ) -> HermesRun:
        self._require(self.capabilities.status)
        _require_handle(handle, previous)
        report = await self._request(lambda: self._transport.status(handle, context))
        return self._observation(report, handle=handle, previous=previous)

    async def progress(
        self, handle: str, context: HermesExecutionContext, *, previous: HermesRun | None = None,
    ) -> str | None:
        self._require(self.capabilities.progress)
        return (await self.status(handle, context, previous=previous)).progress

    async def reconcile(
        self,
        context: HermesExecutionContext,
        *,
        correlation_id: str,
        handle: str | None = None,
        previous: HermesRun | None = None,
    ) -> HermesRun | None:
        """Read upstream evidence without dispatching. None leaves intent unresolved."""
        _require_id(correlation_id)
        try:
            if handle is not None and self.capabilities.status:
                return await self.status(handle, context, previous=previous)
            if not self.capabilities.correlation_lookup:
                return None
            report = await self._request(lambda: self._transport.lookup(correlation_id, context))
        except HermesError as error:
            if error.code in (HermesErrorCode.NO_RUN_EVIDENCE, HermesErrorCode.REMOTE_OUTCOME_UNKNOWN):
                return None
            raise
        if report is None:
            return None
        return self._observation(report, handle=handle, previous=previous)

    async def cancel(
        self, handle: str, context: HermesExecutionContext, *, previous: HermesRun | None = None,
    ) -> HermesRun | None:
        """Return None for acknowledgement without a confirmed state change."""
        self._require(self.capabilities.cancellation)
        _require_handle(handle, previous)
        code = HermesErrorCode.CANCEL_UNCERTAIN
        report = await self._request(
            lambda: self._transport.cancel(handle, context), uncertain=code,
        )
        if report is None:
            return None
        return self._observation(report, handle=handle, previous=previous, error_code=code)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._close_task)

    async def _shutdown(self) -> None:
        active = tuple(self._active)
        for operation in active:
            operation.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        try:
            async with asyncio.timeout(self._timeout):
                await self._transport.close()
        except Exception:
            raise HermesError(HermesErrorCode.UNAVAILABLE) from None

    async def _request[T](
        self,
        start: Callable[[], Coroutine[Any, Any, T]],
        *,
        uncertain: HermesErrorCode | None = None,
        correlation_id: str | None = None,
    ) -> T:
        operation = None
        try:
            async with asyncio.timeout(self._timeout), self._slots:
                if self._close_task is not None:
                    raise HermesError(HermesErrorCode.CLOSED)
                operation = asyncio.create_task(start())
                self._active.add(operation)
                try:
                    return await operation
                finally:
                    self._active.discard(operation)
        except asyncio.CancelledError:
            # Respect cancellation of the caller. A cancelled child with a live
            # caller has lost its chance to report any upstream side effect.
            if asyncio.current_task().cancelling():
                raise
            code = uncertain or HermesErrorCode.UNAVAILABLE
        except HermesError as error:
            code = error.code
            if uncertain and code in (HermesErrorCode.UNAVAILABLE, HermesErrorCode.INVALID_RESPONSE):
                code = uncertain
        except Exception:
            code = uncertain if operation is not None and uncertain else HermesErrorCode.UNAVAILABLE
        raise HermesError(code, correlation_id=correlation_id) from None

    def _observation(
        self,
        report: object,
        *,
        handle: str | None = None,
        previous: HermesRun | None = None,
        error_code: HermesErrorCode = HermesErrorCode.INVALID_RESPONSE,
        correlation_id: str | None = None,
    ) -> HermesRun:
        if (
            not isinstance(report, HermesRun)
            or not _valid_id(report.handle)
            or (handle is not None and handle != report.handle)
            or not isinstance(report.state, AgentRunState)
            or not _bounded_text(report.result, _MAX_RESULT)
            or (report.result is not None and report.state != AgentRunState.COMPLETED)
            or not _bounded_text(report.progress, _MAX_PROGRESS)
        ):
            raise HermesError(error_code, correlation_id=correlation_id)
        if previous is not None:
            if previous.handle != report.handle:
                raise HermesError(HermesErrorCode.INVALID_REQUEST)
            if not AGENT_RUN_TRANSITIONS[previous.state]:
                return previous
            if _ACTIVE_ORDER.get(report.state, 3) < _ACTIVE_ORDER[previous.state]:
                return previous
        if report.progress is not None and not self.capabilities.progress:
            return replace(report, progress=None)
        return report

    @staticmethod
    def _require(supported: bool) -> None:
        if not supported:
            raise HermesError(HermesErrorCode.UNSUPPORTED)


def _bounded_text(value: object, maximum: int) -> bool:
    return value is None or isinstance(value, str) and len(value) <= maximum


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None


def _require_id(value: object) -> None:
    if not _valid_id(value):
        raise HermesError(HermesErrorCode.INVALID_REQUEST)


def _require_handle(handle: str, previous: HermesRun | None) -> None:
    _require_id(handle)
    if previous is not None and previous.handle != handle:
        raise HermesError(HermesErrorCode.INVALID_REQUEST)
