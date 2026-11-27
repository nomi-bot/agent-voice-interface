"""Authenticated durable-run operations, independent of a voice connection."""

import asyncio
import json
from math import isfinite
from typing import Literal, Protocol

from avi.core.durable_auth import AccessDenied, AuthenticatedConnection, LoopbackCredentials, RunPermission
from avi.core.durable_store import (
    AdmissionsClosed,
    DispatchIntent,
    DispatchState,
    JsonValue,
    Observation,
    RecentRun,
    RunReplay,
    RunResult,
    RunSnapshot,
    SQLiteRunStore,
    canonical_task,
)
from avi.core.legacy_cutover import LegacyCutover
from avi.core.lifecycle import AGENT_RUN_TRANSITIONS

type DispatchOutcome = Literal["denied", "unsupported", "failed", "uncertain"]


class DispatchError(Exception):
    """A backend error classified according to its confirmed dispatch evidence."""

    def __init__(self, outcome: DispatchOutcome):
        if outcome not in {"denied", "unsupported", "failed", "uncertain"}:
            raise ValueError("Invalid dispatch outcome")
        self.outcome = outcome
        super().__init__(f"Provider operation {outcome}")


class SubmissionQueueFull(RuntimeError):
    """All process-owned wire admission slots are occupied."""


class Unsupported(RuntimeError):
    pass


class DurableBackend(Protocol):
    cancellation: bool

    async def submit(
        self, owner: str, *, kind: str, arguments: dict[str, JsonValue], correlation_id: str,
    ) -> Observation: ...

    async def reconcile(
        self, owner: str, *, correlation_id: str, handle: str | None,
    ) -> Observation | None: ...

    async def cancel(self, owner: str, handle: str) -> Observation | None: ...


class DurableRunService:
    def __init__(
        self,
        store: SQLiteRunStore,
        credentials: LoopbackCredentials,
        backend: DurableBackend,
        *,
        backend_scope: str,
        admissions_open: bool = False,
        legacy_cutover: LegacyCutover | None = None,
        operation_timeout: float = 30,
        max_wire_submissions: int = 32,
    ):
        if not isfinite(operation_timeout) or operation_timeout <= 0:
            raise ValueError("Operation timeout must be positive and finite")
        if not isinstance(backend_scope, str) or not 0 < len(backend_scope) <= 128:
            raise ValueError("Backend scope must be a stable nonsecret identifier")
        if type(max_wire_submissions) is not int or max_wire_submissions < 1:
            raise ValueError("Wire submission capacity must be a positive integer")
        self._max_wire_submissions = max_wire_submissions
        self._wire_submissions: set[asyncio.Task[RunSnapshot]] = set()
        self._backend_scope = backend_scope
        self._store = store
        self._credentials = credentials
        self._backend = backend
        self._admissions_open = admissions_open
        self._legacy_cutover = legacy_cutover
        self._operation_timeout = operation_timeout

    def _allows_admission(self) -> bool:
        return self._admissions_open and (
            self._legacy_cutover is None or self._legacy_cutover.durable_enabled
        )

    async def submit_from_socket(
        self, connection: AuthenticatedConnection | None, request_id: str, kind: str,
        arguments: dict[str, JsonValue],
    ) -> RunSnapshot:
        """Keep one admitted operation alive if its waiting socket closes."""
        if len(self._wire_submissions) >= self._max_wire_submissions:
            raise SubmissionQueueFull("Wire submission capacity exhausted")
        task = asyncio.create_task(self.submit(connection, request_id, kind, arguments))
        self._wire_submissions.add(task)
        task.add_done_callback(self._wire_submission_done)
        return await asyncio.shield(task)

    def _wire_submission_done(self, task: asyncio.Task[RunSnapshot]) -> None:
        self._wire_submissions.discard(task)
        if not task.cancelled():
            task.exception()  # A disconnected peer may no longer be awaiting it.

    async def settle_submissions(self) -> None:
        """Drain issued operations after closing admissions and before backend shutdown."""
        if self._wire_submissions:
            await asyncio.gather(*tuple(self._wire_submissions), return_exceptions=True)

    async def submit(
        self,
        connection: AuthenticatedConnection | None,
        request_id: str,
        kind: str,
        arguments: dict[str, JsonValue],
    ) -> RunSnapshot:
        owner = self._credentials.require(connection, RunPermission.SUBMIT)
        # Take a private copy before awaiting. The sent content must match the
        # digest even if caller-owned arguments change during the database write.
        copied_arguments = json.loads(canonical_task(kind, arguments))["arguments"]
        admissions_open = self._allows_admission()
        reserved = await asyncio.to_thread(
            self._store.reserve, owner, request_id, kind, copied_arguments,
            allow_new=admissions_open, backend_scope=self._backend_scope,
        )
        run_id = reserved.run_id
        if not admissions_open or not await asyncio.to_thread(
            self._store.claim_dispatch, run_id, backend_scope=self._backend_scope,
        ):
            intent = await asyncio.to_thread(self._store.intent, owner, run_id)
            self._credentials.require(connection, RunPermission.SUBMIT)
            return intent.status

        try:
            self._credentials.require(connection, RunPermission.SUBMIT)
        except AccessDenied:
            await asyncio.to_thread(self._store.dispatch_error, run_id, outcome="denied")
            raise
        # Policy can change while the reserve or claim transaction is awaited.
        # We own this claim, but no provider call has been made.
        if not self._allows_admission():
            await asyncio.to_thread(self._store.dispatch_error, run_id, outcome="denied")
            raise AdmissionsClosed("Durable submissions are disabled")

        try:
            async with asyncio.timeout(self._operation_timeout):
                observation = await self._backend.submit(
                    owner, kind=kind, arguments=copied_arguments, correlation_id=run_id,
                )
            if not isinstance(observation, Observation):
                raise DispatchError("uncertain")
        except DispatchError as error:
            snapshot = await asyncio.to_thread(self._store.dispatch_error, run_id, outcome=error.outcome)
        except asyncio.CancelledError:
            await asyncio.to_thread(self._store.dispatch_error, run_id, outcome="uncertain")
            raise
        except Exception:
            # An unclassified transport failure supplies no evidence that the
            # remote side rejected the task, so this submission cannot be retried.
            snapshot = await asyncio.to_thread(self._store.dispatch_error, run_id, outcome="uncertain")
        else:
            snapshot = await asyncio.to_thread(self._store.record_observation, run_id, observation)
        self._credentials.require(connection, RunPermission.SUBMIT)
        return snapshot

    async def lookup_admitted(
        self, connection: AuthenticatedConnection | None, request_id: str,
        kind: str, arguments: dict[str, JsonValue],
    ) -> RunSnapshot | None:
        """Read an existing dispatch for the authenticated owner; never submit."""
        owner = self._credentials.require(connection, RunPermission.STATUS)
        copied_arguments = json.loads(canonical_task(kind, arguments))["arguments"]
        snapshot = await asyncio.to_thread(
            self._store.lookup_admitted, owner, request_id, kind, copied_arguments,
            backend_scope=self._backend_scope,
        )
        self._credentials.require(connection, RunPermission.STATUS)
        return snapshot

    async def recent(self, connection: AuthenticatedConnection | None) -> tuple[RecentRun, ...]:
        permissions = (RunPermission.DISCOVER, RunPermission.STATUS)
        owner = self._credentials.require(connection, *permissions)
        runs = await asyncio.to_thread(self._store.recent, owner)
        self._credentials.require(connection, *permissions)
        return runs

    async def status(self, connection: AuthenticatedConnection | None, run_id: str) -> RunSnapshot:
        owner = self._credentials.require(connection, RunPermission.STATUS)
        intent = await asyncio.to_thread(self._store.intent, owner, run_id)
        self._credentials.require(connection, RunPermission.STATUS)
        return intent.status

    async def result(self, connection: AuthenticatedConnection | None, run_id: str) -> RunResult:
        owner = self._credentials.require(connection, RunPermission.RESULT)
        result = await asyncio.to_thread(self._store.result, owner, run_id)
        self._credentials.require(connection, RunPermission.RESULT)
        return result

    async def attach(
        self, connection: AuthenticatedConnection | None, run_id: str, *, after_cursor: int = 0,
    ) -> RunReplay:
        permissions = (RunPermission.ATTACH, RunPermission.PROGRESS, RunPermission.RESULT)
        owner = self._credentials.require(connection, *permissions)
        replay = await asyncio.to_thread(
            self._store.replay, owner, run_id, after_cursor=after_cursor, include_result=True,
        )
        self._credentials.require(connection, *permissions)
        return replay

    async def progress(
        self, connection: AuthenticatedConnection | None, run_id: str, *, after_cursor: int = 0,
    ) -> RunReplay:
        owner = self._credentials.require(connection, RunPermission.PROGRESS)
        replay = await asyncio.to_thread(
            self._store.replay, owner, run_id, after_cursor=after_cursor, include_result=False,
        )
        self._credentials.require(connection, RunPermission.PROGRESS)
        return replay

    async def cancel(self, connection: AuthenticatedConnection | None, run_id: str) -> RunSnapshot:
        owner = self._credentials.require(connection, RunPermission.CANCEL)
        intent = await asyncio.to_thread(self._store.intent, owner, run_id)
        self._credentials.require(connection, RunPermission.CANCEL)
        if not AGENT_RUN_TRANSITIONS[intent.status.state]:
            return intent.status
        if intent.status.dispatch is DispatchState.RESERVED:
            snapshot = await asyncio.to_thread(self._store.cancel_reservation, run_id)
            self._credentials.require(connection, RunPermission.CANCEL)
            if not AGENT_RUN_TRANSITIONS[snapshot.state]:
                return snapshot
            intent = await asyncio.to_thread(self._store.intent, owner, run_id)
            self._credentials.require(connection, RunPermission.CANCEL)
        snapshot = await self._cancel_remote(intent)
        self._credentials.require(connection, RunPermission.CANCEL)
        return snapshot

    async def _cancel_remote(self, intent: DispatchIntent) -> RunSnapshot:
        if (
            not self._backend.cancellation or intent.handle is None
            or intent.backend_scope != self._backend_scope
        ):
            raise Unsupported("Cancellation unavailable")
        try:
            async with asyncio.timeout(self._operation_timeout):
                observation = await self._backend.cancel(intent.owner, intent.handle)
            if observation is not None and (
                not isinstance(observation, Observation) or observation.handle != intent.handle
            ):
                raise DispatchError("uncertain")
        except DispatchError:
            raise
        except Exception:
            raise DispatchError("uncertain") from None
        if observation is None:
            return (await asyncio.to_thread(self._store.intent, intent.owner, intent.status.run_id)).status
        return await asyncio.to_thread(self._store.record_observation, intent.status.run_id, observation)

    async def recover(self, *, startup: bool = True) -> None:
        """Read provider evidence. Startup recovery precedes new admissions."""
        for intent in await asyncio.to_thread(self._store.pending):
            run_id = intent.status.run_id
            if intent.status.dispatch is DispatchState.RESERVED:
                continue
            if intent.status.dispatch is DispatchState.DISPATCHING:
                if not startup:
                    continue
                await asyncio.to_thread(self._store.dispatch_error, run_id, outcome="uncertain")
            if intent.backend_scope != self._backend_scope:
                await asyncio.to_thread(self._store.mark_reconciliation_pending, run_id)
                continue
            try:
                async with asyncio.timeout(self._operation_timeout):
                    observation = await self._backend.reconcile(
                        intent.owner, correlation_id=run_id, handle=intent.handle,
                    )
            except Exception:
                observation = None
            if isinstance(observation, Observation) and intent.handle in (None, observation.handle):
                await asyncio.to_thread(self._store.record_observation, run_id, observation)
            else:
                await asyncio.to_thread(self._store.mark_reconciliation_pending, run_id)

    async def maintain(self) -> None:
        """Apply expiry and prune results and deduplication metadata."""
        for pending in await asyncio.to_thread(self._store.pending):
            if not pending.status.expired:
                continue
            intent = await asyncio.to_thread(
                self._store.note_expiry, pending.status.run_id,
                can_cancel=self._backend.cancellation and pending.backend_scope == self._backend_scope,
            )
            if intent is not None:
                try:
                    await self._cancel_remote(intent)
                except (DispatchError, Unsupported):
                    # The store keeps the active outcome and records the attempted expiry action.
                    pass
        await asyncio.to_thread(self._store.prune)
