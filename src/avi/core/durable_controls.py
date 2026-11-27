"""One negotiated AVIP durable control channel and its server-held credential."""

from collections.abc import AsyncIterator, Callable
from dataclasses import asdict

from avi.core.durable_auth import AccessDenied, AuthenticatedConnection, LoopbackCredentials, RunPermission
from avi.core.durable_runs import DispatchError, DurableRunService, SubmissionQueueFull, Unsupported
from avi.core.durable_store import AdmissionsClosed, InvalidCursor, RunConflict
from avi.core.voice_delegation import READ_ONLY, valid_read_only_query
from avi.protocols.avip import PROTOCOL, ProtocolError, error_envelope, validate_identifier


_FIELDS = {
    "agent.run.authenticate": {"token"},
    "agent.run.submit": {"request_id", "kind", "arguments"},
    "agent.run.status": {"run_id"},
    "agent.run.result": {"run_id"},
    "agent.run.recent": set(),
    "agent.run.progress": {"run_id", "after_cursor"},
    "agent.run.attach": {"run_id", "after_cursor"},
    "agent.run.cancel": {"run_id"},
}


def _envelope(operation: str, **fields) -> dict:
    return {"protocol": PROTOCOL, "type": f"agent.run.{operation}", **fields}


class DurableControls:
    def __init__(
        self, service: DurableRunService, credentials: LoopbackCredentials, *,
        approve_query: Callable[[AuthenticatedConnection, str], bool] | None = None,
    ):
        self._service = service
        self._credentials = credentials
        self._approve_query = approve_query
        self.connection: AuthenticatedConnection | None = None

    async def replies(self, message: dict, *, discovery: bool = False) -> AsyncIterator[dict]:
        """Map expected operation errors to stable redacted frames."""
        try:
            async for response in self._route(message, discovery):
                yield response
        except ProtocolError as error:
            yield error_envelope(error)
        except AccessDenied:
            yield error_envelope(ProtocolError("access_denied"))
        except AdmissionsClosed:
            yield error_envelope(ProtocolError("admissions_closed"))
        except RunConflict:
            yield error_envelope(ProtocolError("request_conflict"))
        except InvalidCursor:
            yield error_envelope(ProtocolError("invalid_cursor"))
        except SubmissionQueueFull:
            yield error_envelope(ProtocolError("queue_full"))
        except Unsupported:
            yield error_envelope(ProtocolError("operation_unsupported"))
        except DispatchError as error:
            code = {
                "denied": "access_denied",
                "unsupported": "operation_unsupported",
                "failed": "operation_failed",
                "uncertain": "outcome_uncertain",
            }[error.outcome]
            yield error_envelope(ProtocolError(code))

    async def _route(self, message: dict, discovery: bool) -> AsyncIterator[dict]:
        operation = message["type"]
        if operation not in _FIELDS or (operation == "agent.run.recent" and not discovery):
            raise ProtocolError("unsupported_message_type", "type")
        if message.get("protocol") != PROTOCOL:
            raise ProtocolError("unsupported_protocol", "protocol")
        if message.keys() != _FIELDS[operation] | {"type", "protocol"}:
            raise ProtocolError("invalid_message")
        if operation == "agent.run.authenticate":
            if self.connection is not None:
                raise AccessDenied()
            self.connection = self._credentials.authenticate(message["token"])
            yield _envelope("authenticated")
            return
        self._credentials.require(self.connection)
        if operation == "agent.run.recent":
            runs = await self._service.recent(self.connection)
            yield _envelope("recent", runs=[{"run_id": run.run_id, "state": run.state} for run in runs])
            return
        if operation == "agent.run.submit":
            request_id = validate_identifier(message["request_id"], "request_id")
            if message["kind"] != READ_ONLY:
                raise ProtocolError("operation_unsupported")
            args = message["arguments"]
            if not isinstance(args, dict) or args.keys() != {"query"} or not valid_read_only_query(args["query"]):
                raise ProtocolError("invalid_message", "arguments")
            self._credentials.require(self.connection, RunPermission.SUBMIT)
            if self._approve_query is None or not self._approve_query(self.connection, args["query"]):
                raise AccessDenied()
            status = await self._service.submit_from_socket(self.connection, request_id, READ_ONLY, args)
            yield _envelope("status", **asdict(status))
            return
        run_id = validate_identifier(message["run_id"], "run_id")
        if operation == "agent.run.status":
            yield _envelope("status", **asdict(await self._service.status(self.connection, run_id)))
        elif operation == "agent.run.cancel":
            yield _envelope("status", **asdict(await self._service.cancel(self.connection, run_id)))
        elif operation == "agent.run.result":
            yield _envelope("result", **asdict(await self._service.result(self.connection, run_id)))
        else:
            cursor = message["after_cursor"]
            if type(cursor) is not int or not 0 <= cursor < 2**53:
                raise ProtocolError("invalid_cursor")
            if operation == "agent.run.attach":
                permissions = (RunPermission.ATTACH, RunPermission.PROGRESS, RunPermission.RESULT)
                replay = await self._service.attach(self.connection, run_id, after_cursor=cursor)
            else:
                permissions = (RunPermission.PROGRESS,)
                replay = await self._service.progress(self.connection, run_id, after_cursor=cursor)
            if replay.snapshot is not None:
                yield _envelope("snapshot", **asdict(replay.snapshot))
            for event in replay.events:
                self._credentials.require(self.connection, *permissions)
                yield _envelope("event", **asdict(event))
            self._credentials.require(self.connection, *permissions)
            yield _envelope("replay", run_id=run_id, resume_cursor=replay.resume_cursor)
