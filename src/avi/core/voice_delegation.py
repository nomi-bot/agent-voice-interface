"""Connection-scoped voice routing over the authorized durable-run service."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from math import isfinite
import re
from typing import Protocol

from avi.core.durable_auth import RunPermission
from avi.core.events import VoiceEvent
from avi.core.lifecycle import AgentRunState
from avi.core.voice_tool_journal import VoiceCall, VoiceCallConflict, VoiceToolJournal


DELEGATE_TOOL = "avi_delegate_read_only"
RESULT_TOOL = "avi_read_run_result"
ACK_TOOL = "avi_ack"
READ_ONLY = "read_only"
_MAX_CANONICAL_TASK_BYTES = 4096
_MAX_SPOKEN_RESULT_CHARS = 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_CALL_ID = re.compile(r"[!-~]{1,256}")
logger = logging.getLogger(__name__)


class VoiceToolOverload(RuntimeError):
    """The gateway reached the connection's pending voice-tool limit."""


class ToolResponder(Protocol):
    async def tool_result(self, call_id: str, result: dict) -> None: ...


class RunSnapshot(Protocol):
    @property
    def run_id(self) -> str: ...
    @property
    def state(self) -> AgentRunState: ...
    @property
    def failure_code(self) -> str | None: ...
    @property
    def reconciliation_pending(self) -> bool: ...


class RunResult(Protocol):
    @property
    def status(self) -> RunSnapshot: ...
    @property
    def result(self) -> str | None: ...
    @property
    def retained(self) -> bool: ...


class VoiceRuns[Connection](Protocol):
    async def submit(
        self, connection: Connection, request_id: str, kind: str, arguments: dict[str, str],
    ) -> RunSnapshot: ...
    async def result(self, connection: Connection, run_id: str) -> RunResult: ...
    async def lookup_admitted(
        self, connection: Connection, request_id: str, kind: str, arguments: dict[str, str],
    ) -> RunSnapshot | None: ...


@dataclass(frozen=True)
class _ToolInput:
    name: str
    value: str = ""
    error: str = ""

    @property
    def fingerprint(self) -> str:
        return _digest([self.name, self.value, self.error])


@dataclass
class _Pending:
    task: asyncio.Task[None]
    inputs: _ToolInput
    expired: bool = False


class VoiceToolSession[Connection]:
    """Dispatch provider events in bounded tasks outside the audio pump."""

    def __init__(
        self,
        *,
        session_id: str,
        connection: Connection | None,
        provider: ToolResponder,
        runs: VoiceRuns[Connection],
        approve_query: Callable[[Connection, str], bool],
        journal: VoiceToolJournal,
        run_conflict: type[Exception],
        admissions_closed: type[Exception],
        authorize_reply: Callable[[Connection | None, RunPermission], object] | None = None,
        operation_timeout: float = 8,
        max_pending_calls: int = 16,
    ):
        if not _identifier(session_id):
            raise ValueError("Invalid voice session identifier")
        if not isfinite(operation_timeout) or operation_timeout <= 0:
            raise ValueError("Voice tool timeout must be finite and positive")
        if type(max_pending_calls) is not int or max_pending_calls < 1:
            raise ValueError("Voice tool limit must be a positive integer")
        self._session_id = session_id
        self._connection = connection
        self._provider = provider
        self._runs = runs
        self._approve_query = approve_query
        self._authorize_reply = authorize_reply
        self._journal = journal
        self._run_conflict = run_conflict
        self._admissions_closed = admissions_closed
        self._timeout = operation_timeout
        self._max_pending_calls = max_pending_calls
        self._active: dict[str, _Pending] = {}
        self._closed = False

    def bind_connection(self, connection: Connection) -> None:
        """Bind an authenticated socket grant once; calls keep their arrival-time grant."""
        if self._connection is not None and self._connection != connection:
            raise PermissionError("Voice tool principal cannot change")
        self._connection = connection

    def accept(self, event: VoiceEvent, *, turn_id: str, response_id: str) -> asyncio.Task[None] | None:
        """Schedule one tool call or consume provider tool expiry; never wait for a run."""
        payload = event.payload
        if self._closed or not isinstance(payload, dict):
            return None
        if event.kind == "error" and payload.get("code") == "tool_call_cancelled":
            self._expire(payload.get("ids"))
            return None
        if event.kind != "tool_call":
            return None
        call_id = payload.get("id")
        if not isinstance(call_id, str) or _CALL_ID.fullmatch(call_id) is None:
            raise ValueError("Invalid provider tool call identifier")
        if not _identifier(turn_id) or not _identifier(response_id):
            raise ValueError("Invalid voice turn or response identifier")
        inputs = _parse_tool(payload)
        if pending := self._active.get(call_id):
            if pending.inputs != inputs:
                raise VoiceCallConflict("Voice tool call payload changed")
            return pending.task
        if len(self._active) >= self._max_pending_calls:
            raise VoiceToolOverload("Too many pending voice tool calls")
        request_id = "voice-" + _digest([self._session_id, call_id])
        call = VoiceCall(self._session_id, turn_id, response_id, call_id, request_id)
        task = asyncio.create_task(self._dispatch(call, inputs, self._connection))
        self._active[call_id] = _Pending(task, inputs)
        task.add_done_callback(lambda done: self._finished(call_id, done))
        return task

    def close(self) -> None:
        """Detach this provider connection. In-flight admitted work can finish."""
        self._closed = True

    async def detach(self) -> None:
        """Start the journal's retry window when the gateway socket closes."""
        self.close()
        await asyncio.to_thread(self._journal.close_session, self._session_id)

    def _expire(self, ids: object) -> None:
        if isinstance(ids, (tuple, list)):
            for call_id in ids[: self._max_pending_calls]:
                if isinstance(call_id, str) and (pending := self._active.get(call_id)):
                    pending.expired = True

    def _finished(self, call_id: str, task: asyncio.Task[None]) -> None:
        self._active.pop(call_id, None)
        failure = None if task.cancelled() else task.exception()
        if failure is not None and not isinstance(failure, VoiceCallConflict):
            logger.warning("Voice tool call failed")

    def _expired(self, call: VoiceCall) -> bool:
        return self._closed or self._active[call.call_id].expired

    async def _dispatch(
        self, call: VoiceCall, inputs: _ToolInput, connection: Connection | None,
    ) -> None:
        first_delivery, saved = await asyncio.to_thread(self._journal.begin, call, inputs.fingerprint)
        if not first_delivery:
            if saved.run_id is None and saved.request_id == call.request_id:
                await self._recover_admission(call, inputs, connection)
            return
        if self._expired(call):
            await asyncio.to_thread(self._journal.transition, call, "pending", "expired")
            return
        reply, run_id = await self._reply(call, inputs, connection)
        if run_id is not None:
            await asyncio.to_thread(self._journal.bind_run, call, run_id)
        if self._expired(call):
            await asyncio.to_thread(self._journal.transition, call, "pending", "expired")
            return
        if not await asyncio.to_thread(self._journal.transition, call, "pending", "attempted"):
            return
        if self._expired(call):
            await asyncio.to_thread(self._journal.transition, call, "attempted", "expired")
            return
        # Journal writes above may outlive the credential. Authorize the private
        # payload at the last synchronous boundary before handing it to Gemini.
        if self._authorize_reply is not None and inputs.name in (DELEGATE_TOOL, RESULT_TOOL):
            permission = RunPermission.RESULT if inputs.name == RESULT_TOOL else RunPermission.SUBMIT
            try:
                self._authorize_reply(connection, permission)
            except PermissionError:
                reply = _error("access_denied")
        try:
            async with asyncio.timeout(self._timeout):
                await self._provider.tool_result(call.call_id, reply)
        except Exception:
            # The send may already have reached the provider. Retain the attempt.
            return
        await asyncio.to_thread(self._journal.transition, call, "attempted", "acknowledged")

    async def _recover_admission(
        self, call: VoiceCall, inputs: _ToolInput, connection: Connection | None,
    ) -> None:
        """Rebind private bookkeeping only, never answer a possibly stale turn."""
        if connection is None or inputs.name != DELEGATE_TOOL or inputs.error:
            return
        try:
            async with asyncio.timeout(self._timeout):
                snapshot = await self._runs.lookup_admitted(
                    connection, call.request_id, READ_ONLY, {"query": inputs.value},
                )
        except (PermissionError, TimeoutError):
            return
        if snapshot is not None:
            await asyncio.to_thread(self._journal.bind_run, call, snapshot.run_id)

    async def _reply(
        self, call: VoiceCall, inputs: _ToolInput, connection: Connection | None,
    ) -> tuple[dict, str | None]:
        if inputs.error:
            return _error(inputs.error), None
        if inputs.name == ACK_TOOL:
            return {"output": {}}, None
        if connection is None:
            return _error("access_denied"), None
        try:
            async with asyncio.timeout(self._timeout):
                if inputs.name == DELEGATE_TOOL:
                    if not self._approve_query(connection, inputs.value):
                        return _error("approval_denied"), None
                    snapshot = await self._runs.submit(
                        connection, call.request_id, READ_ONLY, {"query": inputs.value},
                    )
                    return _status(snapshot), snapshot.run_id
                result = await self._runs.result(connection, inputs.value)
                return _result(result), result.status.run_id
        except PermissionError:
            return _error("access_denied"), None
        except self._run_conflict:
            return _error("request_conflict"), None
        except self._admissions_closed:
            return _error("submissions_disabled"), None
        except TimeoutError:
            if inputs.name == DELEGATE_TOOL:
                return {"output": {"status": "pending_reconciliation"}}, None
            return _error("result_unavailable"), None
        except Exception:
            return _error("run_service_unavailable"), None


def _parse_tool(payload: dict) -> _ToolInput:
    name = payload.get("name")
    args = payload.get("args")
    if name == ACK_TOOL:
        return _ToolInput(ACK_TOOL)
    if name == DELEGATE_TOOL:
        if isinstance(args, dict) and args.keys() == {"query"} and valid_read_only_query(args["query"]):
            return _ToolInput(DELEGATE_TOOL, args["query"])
        return _ToolInput(DELEGATE_TOOL, error="invalid_arguments")
    if name == RESULT_TOOL:
        if isinstance(args, dict) and args.keys() == {"run_id"} and _identifier(args["run_id"]):
            return _ToolInput(RESULT_TOOL, args["run_id"])
        return _ToolInput(RESULT_TOOL, error="invalid_arguments")
    return _ToolInput("", error="unknown_tool")


def valid_read_only_query(query: object) -> bool:
    if not isinstance(query, str) or len(query) > _MAX_CANONICAL_TASK_BYTES or not query.strip():
        return False
    try:
        query.encode("utf-8")
    except UnicodeEncodeError:
        return False
    payload = json.dumps({"kind": READ_ONLY, "arguments": {"query": query}}, separators=(",", ":"))
    return len(payload) <= _MAX_CANONICAL_TASK_BYTES


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _digest(parts: list[str]) -> str:
    return sha256(json.dumps(parts, separators=(",", ":")).encode("ascii")).hexdigest()


def _error(code: str) -> dict:
    return {"error": {"code": code}}


def _status(snapshot: RunSnapshot) -> dict:
    if snapshot.state == AgentRunState.FAILED:
        match snapshot.failure_code:
            case "dispatch_denied":
                return _error("approval_denied")
            case "dispatch_unsupported":
                return _error("unsupported")
        return _error("run_failed")
    state = "pending_reconciliation" if snapshot.reconciliation_pending else snapshot.state.value
    return {"output": {"status": state, "run_id": snapshot.run_id}}


def _result(result: RunResult) -> dict:
    reply = _status(result.status)
    if result.status.state == AgentRunState.COMPLETED:
        if not result.retained:
            return _error("result_expired")
        if result.result is not None:
            reply["output"]["result"] = result.result[:_MAX_SPOKEN_RESULT_CHARS]
    return reply
