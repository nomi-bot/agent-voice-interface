"""Admission and drain hook for the single-process legacy control route."""

import asyncio
from math import isfinite
from uuid import uuid4

from avi.agents.base import AgentAdapter, Run
from avi.core.durable_store import AdmissionsClosed, LegacyOutcome, SQLiteRunStore


class CutoverBlocked(RuntimeError):
    pass


class LegacyCutover:
    """Track legacy work at its original adapter until terminal evidence exists."""

    def __init__(self, store: SQLiteRunStore, *, operation_timeout: float = 30):
        if not isfinite(operation_timeout) or operation_timeout <= 0:
            raise ValueError("Operation timeout must be positive and finite")
        self._store = store
        self._operation_timeout = operation_timeout
        self._accepting_legacy = True
        self._durable_enabled = False
        self._submitting = 0
        self._unconfirmed_submission = False
        self._running: dict[tuple[str, str], AgentAdapter] = {}
        self._outcomes: dict[tuple[str, str], LegacyOutcome] = {}
        self._switch = asyncio.Lock()

    @property
    def durable_enabled(self) -> bool:
        return self._durable_enabled

    def track(self, adapter: AgentAdapter) -> AgentAdapter:
        """Wrap each legacy session's adapter before its first submission."""
        return _TrackedLegacyAdapter(self, uuid4().hex, adapter)

    def begin(self) -> None:
        """Leave existing legacy sessions in place and close their admission gate."""
        self._accepting_legacy = False

    async def enable_durable(self, *, cancel: bool = False) -> None:
        """Drain once under the legacy policy and persist terminal evidence."""
        async with self._switch:
            if self._durable_enabled:
                return
            if self._accepting_legacy or self._submitting or self._unconfirmed_submission:
                raise CutoverBlocked("Legacy admissions or submissions are unresolved")
            for (session_id, run_id), adapter in tuple(self._running.items()):
                await self._inspect(session_id, run_id, adapter, cancel=cancel)
            if self._running:
                raise CutoverBlocked("Legacy work has an unconfirmed terminal outcome")
            await asyncio.to_thread(self._store.record_legacy_cutover, tuple(self._outcomes.values()))
            self._outcomes.clear()
            self._durable_enabled = True

    async def rollback(self) -> None:
        """Disable durable admissions and leave the old admission gate frozen."""
        async with self._switch:
            self._durable_enabled = False

    async def _submit(self, session_id: str, adapter: AgentAdapter, request: str) -> Run:
        if not self._accepting_legacy:
            raise AdmissionsClosed("Legacy submissions are disabled")
        self._submitting += 1
        try:
            run = await adapter.submit(request)
        except ValueError:
            # The legacy mock raises this only for pre-admission rejection.
            raise
        except (Exception, asyncio.CancelledError):
            self._unconfirmed_submission = True
            raise
        finally:
            self._submitting -= 1
        self._running[(session_id, run.id)] = adapter
        self._observe(session_id, run)
        return run

    def _observe(self, session_id: str, run: Run) -> None:
        key = (session_id, run.id)
        if run.status in {"completed", "failed", "cancelled"} and key in self._running:
            self._running.pop(key)
            if not self._accepting_legacy:
                self._outcomes[key] = LegacyOutcome(session_id, run.id, run.status)

    async def _inspect(
        self, session_id: str, run_id: str, adapter: AgentAdapter, *, cancel: bool = False,
    ) -> None:
        if cancel and adapter.capabilities.cancellation:
            operation = adapter.cancel
        elif adapter.capabilities.status:
            operation = adapter.status
        else:
            return
        try:
            async with asyncio.timeout(self._operation_timeout):
                run = await operation(run_id)
        except Exception:
            return
        if run.id == run_id:
            self._observe(session_id, run)

    async def _close(self, session_id: str, adapter: AgentAdapter) -> None:
        # The original adapter still owns and performs its disconnect cleanup.
        await adapter.close()
        for owner_session, run_id in tuple(self._running):
            if owner_session == session_id:
                await self._inspect(session_id, run_id, adapter)


class _TrackedLegacyAdapter:
    def __init__(self, cutover: LegacyCutover, session_id: str, adapter: AgentAdapter):
        self._cutover = cutover
        self._session_id = session_id
        self._adapter = adapter
        self.capabilities = adapter.capabilities

    async def submit(self, request: str) -> Run:
        return await self._cutover._submit(self._session_id, self._adapter, request)

    async def status(self, run_id: str) -> Run:
        run = await self._adapter.status(run_id)
        self._cutover._observe(self._session_id, run)
        return run

    async def cancel(self, run_id: str) -> Run:
        run = await self._adapter.cancel(run_id)
        self._cutover._observe(self._session_id, run)
        return run

    async def close(self) -> None:
        await self._cutover._close(self._session_id, self._adapter)
