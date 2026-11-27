"""Admission and drain hook for the single-process legacy control route."""

import asyncio
from math import isfinite
from uuid import uuid4

from avi.agents.base import AgentAdapter, Run, SubmissionRejected
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
        self._accepting_legacy = not store.legacy_frozen()
        self._durable_enabled = False
        self._submitting = 0
        self._running: dict[tuple[str, str], AgentAdapter] = {}
        self._switch = asyncio.Lock()

    @property
    def durable_enabled(self) -> bool:
        return self._durable_enabled

    def track(self, adapter: AgentAdapter) -> AgentAdapter:
        """Wrap each legacy session's adapter before its first submission."""
        return _TrackedLegacyAdapter(self, uuid4().hex, adapter)

    def begin(self) -> None:
        """Leave existing legacy sessions in place and close their admission gate."""
        self._store.freeze_legacy()
        self._accepting_legacy = False

    async def enable_durable(self, *, cancel: bool = False) -> None:
        """Drain once under the legacy policy and persist terminal evidence."""
        async with self._switch:
            if self._durable_enabled:
                return
            if self._accepting_legacy or self._submitting:
                raise CutoverBlocked("Legacy admissions or submissions are unresolved")
            for (session_id, run_id), adapter in tuple(self._running.items()):
                await self._inspect(session_id, run_id, adapter, cancel=cancel)
            if await asyncio.to_thread(self._store.unresolved_legacy):
                raise CutoverBlocked("Legacy work has an unconfirmed terminal outcome")
            self._durable_enabled = True

    async def reconcile_known_run(
        self, session_id: str, run_id: str, original_adapter: AgentAdapter,
    ) -> bool:
        """Trusted operator hook: read terminal evidence using the original legacy scope."""
        async with self._switch:
            unresolved = await asyncio.to_thread(self._store.unresolved_legacy)
            if not any((owner, handle) == (session_id, run_id) for _, owner, handle in unresolved):
                return False
            if not original_adapter.capabilities.status:
                return False
            async with asyncio.timeout(self._operation_timeout):
                observed = await original_adapter.status(run_id)
            if observed.id != run_id or observed.status not in {"completed", "failed", "cancelled"}:
                return False
            outcome = LegacyOutcome(session_id, run_id, observed.status)
            await asyncio.to_thread(self._store.settle_legacy, outcome)
            self._running.pop((session_id, run_id), None)
            return True

    async def rollback(self) -> None:
        """Disable durable admissions and leave the old admission gate frozen."""
        async with self._switch:
            self._durable_enabled = False

    async def _submit(self, session_id: str, adapter: AgentAdapter, request: str) -> Run:
        if not self._accepting_legacy:
            raise AdmissionsClosed("Legacy submissions are disabled")
        self._submitting += 1
        try:
            ticket = await asyncio.to_thread(self._store.reserve_legacy, session_id)
            try:
                run = await adapter.submit(request)
            except SubmissionRejected:
                await asyncio.to_thread(self._store.discard_rejected_legacy, ticket)
                raise
            await asyncio.to_thread(self._store.bind_legacy, ticket, run.id)
            self._running[(session_id, run.id)] = adapter
            await self._observe(session_id, run)
            return run
        finally:
            self._submitting -= 1

    async def _observe(self, session_id: str, run: Run) -> None:
        key = (session_id, run.id)
        if run.status in {"completed", "failed", "cancelled"} and key in self._running:
            outcome = LegacyOutcome(session_id, run.id, run.status)
            await asyncio.to_thread(self._store.settle_legacy, outcome)
            self._running.pop(key, None)

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
            await self._observe(session_id, run)

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
        await self._cutover._observe(self._session_id, run)
        return run

    async def cancel(self, run_id: str) -> Run:
        run = await self._adapter.cancel(run_id)
        await self._cutover._observe(self._session_id, run)
        return run

    async def close(self) -> None:
        await self._cutover._close(self._session_id, self._adapter)
