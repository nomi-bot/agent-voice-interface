"""Loopback Hermes Run API transport for the revision documented in the handoff."""

import json
import re
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx

from avi.agents.hermes import (
    HermesCapabilities,
    HermesError,
    HermesErrorCode,
    HermesExecutionContext,
    HermesRun,
    HermesTask,
)
from avi.core.lifecycle import AgentRunState


_RUN_ID = re.compile(r"run_[0-9a-f]{32}")
_MAX_BODY_BYTES = 96 * 1024
_RUN_STATES = {
    "queued": AgentRunState.QUEUED,
    "running": AgentRunState.RUNNING,
    "waiting_for_approval": AgentRunState.RUNNING,
    "stopping": AgentRunState.RUNNING,
    "completed": AgentRunState.COMPLETED,
    "cancelled": AgentRunState.CANCELLED,
    "failed": AgentRunState.FAILED,
    "interrupted": AgentRunState.FAILED,
}
_ADMISSION_STATES = {"started", "unknown", *_RUN_STATES}


class HermesHttpTransport:
    """Run API for a dedicated Hermes profile configured for read-only work."""

    capabilities = HermesCapabilities(status=True, cancellation=True)

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        origin = _loopback_origin(base_url)
        if not api_key or any(not 33 <= ord(char) <= 126 for char in api_key):
            raise ValueError("Hermes requires a server bearer credential")
        self._client = httpx.AsyncClient(
            base_url=origin,
            headers={"Authorization": f"Bearer {api_key}", "Accept-Encoding": "identity"},
            trust_env=False,
            follow_redirects=False,
            timeout=5,
            transport=transport,
        )

    async def submit(
        self, task: HermesTask, context: HermesExecutionContext, correlation_id: str,
    ) -> HermesRun:
        query = task.arguments.get("query")
        if (
            task.kind != "read_only"
            or set(task.arguments) != {"query"}
            or not isinstance(query, str)
            or not query.strip()
            or len(query) > 4096
        ):
            raise HermesError(HermesErrorCode.INVALID_REQUEST)
        payload = await self._json(
            "POST", "/v1/runs", expected=202, body={"input": query},
            headers={"Idempotency-Key": correlation_id},
        )
        handle = payload.get("run_id")
        status = payload.get("status")
        if not _valid_handle(handle) or not isinstance(status, str) or status not in _ADMISSION_STATES:
            raise HermesError(HermesErrorCode.INVALID_RESPONSE)
        # This receipt can be a replay of a terminal run and carries no result.
        # Save the handle as admission evidence and read the outcome separately.
        return HermesRun(handle, AgentRunState.SUBMITTED)

    async def status(self, handle: str, context: HermesExecutionContext) -> HermesRun:
        payload = await self._json("GET", _run_path(handle), expected=200)
        if payload.get("object") != "hermes.run":
            raise HermesError(HermesErrorCode.INVALID_RESPONSE)
        return _run_observation(payload, handle)

    async def lookup(self, correlation_id: str, context: HermesExecutionContext) -> HermesRun | None:
        raise HermesError(HermesErrorCode.UNSUPPORTED)

    async def cancel(self, handle: str, context: HermesExecutionContext) -> HermesRun | None:
        payload = await self._json("POST", f"{_run_path(handle)}/stop", expected=200)
        report = _run_observation(payload, handle)
        if report.state == AgentRunState.RUNNING:
            return None
        return report

    async def close(self) -> None:
        await self._client.aclose()

    async def _json(
        self,
        method: str,
        path: str,
        *,
        expected: int,
        body: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        async with self._client.stream(method, path, json=body, headers=headers) as response:
            if response.status_code == 401:
                raise HermesError(HermesErrorCode.CREDENTIALS_REJECTED)
            data = await _bounded_json(response)
            if response.status_code != expected:
                raise _api_error(response.status_code, data)
            return data


def _loopback_origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme in ("http", "https")
            and parts.username is None
            and parts.password is None
            and parts.path in ("", "/")
            and not parts.query
            and not parts.fragment
            and parts.port != 0
            and ip_address(parts.hostname or "").is_loopback
        ):
            return f"{parts.scheme}://{parts.netloc}"
    except ValueError:
        pass
    raise ValueError("Hermes origin must be a numeric loopback HTTP origin")


def _valid_handle(handle: object) -> bool:
    return isinstance(handle, str) and _RUN_ID.fullmatch(handle) is not None


def _run_path(handle: str) -> str:
    if not _valid_handle(handle):
        raise HermesError(HermesErrorCode.INVALID_REQUEST)
    return f"/v1/runs/{handle}"


def _run_observation(data: dict, handle: str) -> HermesRun:
    status = data.get("status")
    if data.get("run_id") != handle or not isinstance(status, str):
        raise HermesError(HermesErrorCode.INVALID_RESPONSE)
    if status == "unknown":
        raise HermesError(HermesErrorCode.REMOTE_OUTCOME_UNKNOWN)
    state = _RUN_STATES.get(status)
    if state is None:
        raise HermesError(HermesErrorCode.INVALID_RESPONSE)
    result = None
    if state == AgentRunState.COMPLETED:
        result = data.get("output")
        if not isinstance(result, str):
            raise HermesError(HermesErrorCode.INVALID_RESPONSE)
    return HermesRun(handle, state, result=result)


async def _bounded_json(response: httpx.Response) -> dict:
    if response.headers.get("content-encoding", "identity") != "identity":
        raise HermesError(HermesErrorCode.INVALID_RESPONSE)
    buffer = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=4096):
        if len(buffer) + len(chunk) > _MAX_BODY_BYTES:
            raise HermesError(HermesErrorCode.INVALID_RESPONSE)
        buffer.extend(chunk)
    try:
        value = json.loads(buffer)
    except (ValueError, RecursionError):
        raise HermesError(HermesErrorCode.INVALID_RESPONSE) from None
    if not isinstance(value, dict):
        raise HermesError(HermesErrorCode.INVALID_RESPONSE)
    return value


def _api_error(status: int, data: dict) -> HermesError:
    error = data.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if status == 403 or status == 409 and code == "permission_denied":
        return HermesError(HermesErrorCode.APPROVAL_DENIED)
    if status == 404:
        return HermesError(HermesErrorCode.NO_RUN_EVIDENCE)
    if status == 400 or (status == 409 and code == "idempotency_key_conflict"):
        return HermesError(HermesErrorCode.REQUEST_REJECTED)
    if status == 409 and code == "unknown_execution":
        return HermesError(HermesErrorCode.REMOTE_OUTCOME_UNKNOWN)
    return HermesError(HermesErrorCode.UNAVAILABLE)
