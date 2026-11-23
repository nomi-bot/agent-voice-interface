"""Compose the Hermes adapter with the authenticated durable run service."""

from avi.agents.hermes import (
    HermesAgentAdapter,
    HermesError,
    HermesExecutionContext,
    HermesRun,
    HermesTask,
)
from avi.core.durable_runs import DispatchError
from avi.core.durable_store import JsonValue, Observation


class HermesDurableBackend:
    """Internal backend; only the run service authenticates and authorizes owners.

    Keep durable admission, reconciliation scope and replay in the run service.
    This backend does not expose a route, launch Hermes or decide which tasks to
    authorize. Use the bounded Hermes HTTP transport and a dedicated profile.
    """

    def __init__(self, adapter: HermesAgentAdapter):
        self._adapter = adapter
        self.cancellation = adapter.capabilities.cancellation

    async def submit(
        self, owner: str, *, kind: str, arguments: dict[str, JsonValue], correlation_id: str,
    ) -> Observation:
        try:
            run = await self._adapter.submit(
                HermesTask(kind, arguments), HermesExecutionContext(owner),
                correlation_id=correlation_id,
            )
        except HermesError as error:
            raise _dispatch_error(error) from None
        return _observation(run)

    async def reconcile(
        self, owner: str, *, correlation_id: str, handle: str | None,
    ) -> Observation | None:
        # A missing receipt is not safe to retry or resolve via a fresh submit.
        if handle is None:
            return None
        try:
            run = await self._adapter.reconcile(
                HermesExecutionContext(owner), correlation_id=correlation_id, handle=handle,
            )
        except HermesError as error:
            raise _dispatch_error(error) from None
        return None if run is None else _observation(run)

    async def cancel(self, owner: str, handle: str) -> Observation | None:
        try:
            run = await self._adapter.cancel(handle, HermesExecutionContext(owner))
        except HermesError as error:
            raise _dispatch_error(error) from None
        # Hermes can acknowledge a stop without later cancellation being settled.
        return None if run is None else _observation(run)

    async def close(self) -> None:
        await self._adapter.close()


def _observation(run: HermesRun) -> Observation:
    return Observation(run.handle, run.state, result=run.result, progress=run.progress)


def _dispatch_error(error: HermesError) -> DispatchError:
    match error.outcome:
        case "denied":
            return DispatchError("denied")
        case "unsupported":
            return DispatchError("unsupported")
        case "failed":
            return DispatchError("failed")
        case "uncertain":
            return DispatchError("uncertain")
        case _:
            # Future classifications must not certify non-execution.
            return DispatchError("uncertain")
