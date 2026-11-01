"""VoiceSession, VoiceResponse and AgentRun state contracts.

The machines are synchronous and transport-independent. They do not open
sockets, talk to providers, or cancel one another. Correlation is by id only.

Invalid edges raise ``InvalidTransition`` and leave the machine unchanged.
Cancellation and terminal failure are different states: a response interrupt
is not a failure, and a run cancel is not a failure. Neither rewrites the
other, and closing a voice session does not cancel a run.

``/ws/avip`` uses ``VoiceSession`` for connecting, active and closed. It does
not enter reconnecting, and it does not construct a response or a run.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
# A failure code must not be another outcome. The state is the outcome.
_NOT_A_FAILURE = frozenset({
    "cancelled", "canceled", "interrupted", "completed",
    "queued", "running", "submitted", "requested", "streaming",
    "connecting", "active", "reconnecting", "closing", "closed",
})


class InvalidTransition(Exception):
    """A move the contract does not allow. The machine is unchanged."""

    def __init__(self, kind: str, current: str, requested: str, operation: str):
        self.kind = kind
        self.current = current
        self.requested = requested
        self.operation = operation
        super().__init__(f"{kind} cannot {operation} from {current} to {requested}")


class VoiceSessionState(StrEnum):
    """connecting → active → reconnecting / closing → closed."""

    CONNECTING = "connecting"
    ACTIVE = "active"
    RECONNECTING = "reconnecting"
    CLOSING = "closing"
    CLOSED = "closed"


class SessionCloseReason(StrEnum):
    """Why a session entered closing. This is not a run or response outcome."""

    ENDED = "ended"
    DISCONNECTED = "disconnected"
    FAILED = "failed"
    ABANDONED = "abandoned"


class VoiceResponseState(StrEnum):
    """requested → streaming → completed / interrupted / failed."""

    REQUESTED = "requested"
    STREAMING = "streaming"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class AgentRunState(StrEnum):
    """submitted → queued / running → completed / failed / cancelled."""

    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


VOICE_SESSION_TRANSITIONS: dict[VoiceSessionState, frozenset[VoiceSessionState]] = {
    VoiceSessionState.CONNECTING: frozenset({
        VoiceSessionState.ACTIVE,
        VoiceSessionState.CLOSING,
    }),
    VoiceSessionState.ACTIVE: frozenset({
        VoiceSessionState.RECONNECTING,
        VoiceSessionState.CLOSING,
    }),
    VoiceSessionState.RECONNECTING: frozenset({
        VoiceSessionState.ACTIVE,
        VoiceSessionState.CLOSING,
    }),
    VoiceSessionState.CLOSING: frozenset({VoiceSessionState.CLOSED}),
    VoiceSessionState.CLOSED: frozenset(),
}

VOICE_RESPONSE_TRANSITIONS: dict[VoiceResponseState, frozenset[VoiceResponseState]] = {
    VoiceResponseState.REQUESTED: frozenset({
        VoiceResponseState.STREAMING,
        VoiceResponseState.INTERRUPTED,
        VoiceResponseState.FAILED,
    }),
    VoiceResponseState.STREAMING: frozenset({
        VoiceResponseState.COMPLETED,
        VoiceResponseState.INTERRUPTED,
        VoiceResponseState.FAILED,
    }),
    VoiceResponseState.COMPLETED: frozenset(),
    VoiceResponseState.INTERRUPTED: frozenset(),
    VoiceResponseState.FAILED: frozenset(),
}

AGENT_RUN_TRANSITIONS: dict[AgentRunState, frozenset[AgentRunState]] = {
    AgentRunState.SUBMITTED: frozenset({
        AgentRunState.QUEUED,
        AgentRunState.RUNNING,
        AgentRunState.FAILED,
        AgentRunState.CANCELLED,
    }),
    AgentRunState.QUEUED: frozenset({
        AgentRunState.RUNNING,
        AgentRunState.FAILED,
        AgentRunState.CANCELLED,
    }),
    AgentRunState.RUNNING: frozenset({
        AgentRunState.COMPLETED,
        AgentRunState.FAILED,
        AgentRunState.CANCELLED,
    }),
    AgentRunState.COMPLETED: frozenset(),
    AgentRunState.FAILED: frozenset(),
    AgentRunState.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class Transition:
    """One accepted edge, in the order it happened. There is no clock."""

    revision: int
    source: str
    target: str
    operation: str


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be an identifier")
    return value


def _optional_identifier(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field)


def _failure_code(code: object) -> str:
    if (
        not isinstance(code, str)
        or _FAILURE_CODE.fullmatch(code) is None
        or code in _NOT_A_FAILURE
    ):
        raise ValueError("failure code must name a failure, not another outcome")
    return code


def _close_reason(reason: object) -> SessionCloseReason:
    if not isinstance(reason, SessionCloseReason):
        raise ValueError("close reason must be a SessionCloseReason")
    return reason


class _Machine:
    """Shared fail-closed edge applicator. Subclasses own the meaning of each edge."""

    kind: str
    transitions: Mapping[object, frozenset[object]]

    def __init__(self, state: StrEnum):
        self.state = state
        self.revision = 0
        self._history: list[Transition] = []

    @property
    def history(self) -> tuple[Transition, ...]:
        return tuple(self._history)

    def _move(self, target: StrEnum, operation: str) -> None:
        source = self.state
        allowed = self.transitions.get(source, frozenset())
        if target not in allowed:
            raise InvalidTransition(self.kind, str(source), str(target), operation)
        self.state = target
        self.revision += 1
        self._history.append(Transition(self.revision, str(source), str(target), operation))

    def _edge(self, operation: str, target: StrEnum, *sources: StrEnum) -> None:
        """Take one named edge from a listed source.

        The state table alone is not enough: ``connecting → active`` is
        ``activate``, and ``reconnecting → active`` is ``reattach``. The
        operation has to match the source or the machine stays put.
        """
        allowed = self.transitions.get(self.state, frozenset())
        if self.state not in sources or target not in allowed:
            raise InvalidTransition(self.kind, str(self.state), str(target), operation)
        self._move(target, operation)

    def _is_terminal(self) -> bool:
        allowed = self.transitions.get(self.state)
        return allowed is not None and not allowed

    def _refuse(self, operation: str) -> None:
        raise InvalidTransition(self.kind, str(self.state), str(self.state), operation)


class VoiceSession(_Machine):
    """One voice connection. Closing it does not cancel an agent run."""

    kind = "VoiceSession"
    transitions = VOICE_SESSION_TRANSITIONS

    def __init__(self):
        super().__init__(VoiceSessionState.CONNECTING)
        self.session_id: str | None = None
        self.request_id: str | None = None
        self.device_id: str | None = None
        self.close_reason: SessionCloseReason | None = None

    def activate(
        self,
        session_id: str,
        *,
        request_id: str | None = None,
        device_id: str | None = None,
    ) -> None:
        """connecting → active, after the peer has been told the session exists."""
        if self.state is not VoiceSessionState.CONNECTING:
            raise InvalidTransition(
                self.kind, str(self.state), VoiceSessionState.ACTIVE, "activate",
            )
        session_id = _identifier(session_id, "session_id")
        request_id = _optional_identifier(request_id, "request_id")
        device_id = _optional_identifier(device_id, "device_id")
        self._move(VoiceSessionState.ACTIVE, "activate")
        # Publish the id only after the edge is accepted. A rejected activate
        # leaves the session unnamed and still connecting.
        self.session_id = session_id
        self.request_id = request_id
        self.device_id = device_id

    def lose_transport(self) -> None:
        """active → reconnecting. The session id stays. Runs are not cancelled."""
        self._edge(
            "lose_transport",
            VoiceSessionState.RECONNECTING,
            VoiceSessionState.ACTIVE,
        )

    def reattach(self) -> None:
        """reconnecting → active, on the same session id."""
        self._edge(
            "reattach",
            VoiceSessionState.ACTIVE,
            VoiceSessionState.RECONNECTING,
        )

    def begin_close(self, reason: SessionCloseReason) -> None:
        """Enter closing and freeze the reason. Does not finish cleanup."""
        reason = _close_reason(reason)
        self._edge(
            "begin_close",
            VoiceSessionState.CLOSING,
            VoiceSessionState.CONNECTING,
            VoiceSessionState.ACTIVE,
            VoiceSessionState.RECONNECTING,
        )
        self.close_reason = reason

    def finish_close(self) -> None:
        """closing → closed. The close reason is already frozen."""
        if self.close_reason is None:
            raise InvalidTransition(
                self.kind, str(self.state), VoiceSessionState.CLOSED, "finish_close",
            )
        self._edge(
            "finish_close",
            VoiceSessionState.CLOSED,
            VoiceSessionState.CLOSING,
        )

    def cleanup(self, reason: SessionCloseReason) -> None:
        """Drive a live session to closed, or finish a close already in progress.

        A second cleanup with the same reason is a no-op once closed. A
        different reason is rejected and does not relabel the session.
        """
        reason = _close_reason(reason)
        if self.state is VoiceSessionState.CLOSED:
            if reason is not self.close_reason:
                self._refuse("cleanup")
            return
        if self.state is VoiceSessionState.CLOSING:
            if reason is not self.close_reason:
                self._refuse("cleanup")
        else:
            self.begin_close(reason)
        self.finish_close()


class VoiceResponse(_Machine):
    """One spoken reply. Interrupt, failure and playback stop are distinct."""

    kind = "VoiceResponse"
    transitions = VOICE_RESPONSE_TRANSITIONS

    def __init__(
        self,
        response_id: str,
        *,
        session_id: str,
        turn_id: str,
        request_id: str | None = None,
    ):
        super().__init__(VoiceResponseState.REQUESTED)
        self.response_id = _identifier(response_id, "response_id")
        self.session_id = _identifier(session_id, "session_id")
        self.turn_id = _identifier(turn_id, "turn_id")
        self.request_id = _optional_identifier(request_id, "request_id")
        self.playback_stops = 0
        self.failure_code: str | None = None
        self.cleaned = False

    def start_streaming(self) -> None:
        """requested → streaming. Completion is only legal after this."""
        self._edge(
            "start_streaming",
            VoiceResponseState.STREAMING,
            VoiceResponseState.REQUESTED,
        )

    def complete(self) -> None:
        """streaming → completed."""
        self._edge(
            "complete",
            VoiceResponseState.COMPLETED,
            VoiceResponseState.STREAMING,
        )

    def interrupt(self) -> None:
        """Cancel provider output. The outcome is interrupted, never failed."""
        self._edge(
            "interrupt",
            VoiceResponseState.INTERRUPTED,
            VoiceResponseState.REQUESTED,
            VoiceResponseState.STREAMING,
        )

    def fail(self, code: str = "failed") -> None:
        """Terminal provider failure. The outcome is failed, never interrupted."""
        if VoiceResponseState.FAILED not in self.transitions.get(self.state, frozenset()):
            raise InvalidTransition(
                self.kind, str(self.state), VoiceResponseState.FAILED, "fail",
            )
        failure_code = _failure_code(code)
        self._edge(
            "fail",
            VoiceResponseState.FAILED,
            VoiceResponseState.REQUESTED,
            VoiceResponseState.STREAMING,
        )
        self.failure_code = failure_code

    def stop_playback(self) -> None:
        """Count a client audio flush.

        This does not change the response state, does not interrupt generation,
        and does not cancel an agent run.
        """
        self.playback_stops += 1

    def cleanup(self) -> None:
        """Release a terminal response. Refuses a response that is still open.

        Cleanup does not cancel or fail the response, and repeating it does
        not change the terminal state.
        """
        if not self._is_terminal():
            self._refuse("cleanup")
        self.cleaned = True


class AgentRun(_Machine):
    """One delegated task. Cancel and failure are different terminal states.

    A voice-session close does not call this machine. Discarding a live run
    requires ``cancel`` or ``fail`` first; ``cleanup`` will not choose one.
    """

    kind = "AgentRun"
    transitions = AGENT_RUN_TRANSITIONS

    def __init__(
        self,
        run_id: str,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
    ):
        super().__init__(AgentRunState.SUBMITTED)
        self.run_id = _identifier(run_id, "run_id")
        self.session_id = _optional_identifier(session_id, "session_id")
        self.request_id = _optional_identifier(request_id, "request_id")
        self.result: str | None = None
        self.failure_code: str | None = None
        self.cleaned = False

    def enqueue(self) -> None:
        """submitted → queued."""
        self._edge("enqueue", AgentRunState.QUEUED, AgentRunState.SUBMITTED)

    def start(self) -> None:
        """submitted or queued → running. A direct start skips the queue."""
        self._edge(
            "start",
            AgentRunState.RUNNING,
            AgentRunState.SUBMITTED,
            AgentRunState.QUEUED,
        )

    def complete(self, result: str | None = None) -> None:
        """running → completed. ``result`` is optional and is not a status."""
        if self.state is not AgentRunState.RUNNING:
            raise InvalidTransition(
                self.kind, str(self.state), AgentRunState.COMPLETED, "complete",
            )
        if result is not None and (not isinstance(result, str) or len(result) > 4096):
            raise ValueError("result must be omitted or a string of at most 4096 characters")
        self._edge("complete", AgentRunState.COMPLETED, AgentRunState.RUNNING)
        self.result = result

    def fail(self, code: str = "failed") -> None:
        """Terminal failure. The outcome is failed, never cancelled."""
        if AgentRunState.FAILED not in self.transitions.get(self.state, frozenset()):
            raise InvalidTransition(
                self.kind, str(self.state), AgentRunState.FAILED, "fail",
            )
        failure_code = _failure_code(code)
        self._edge(
            "fail",
            AgentRunState.FAILED,
            AgentRunState.SUBMITTED,
            AgentRunState.QUEUED,
            AgentRunState.RUNNING,
        )
        self.failure_code = failure_code

    def cancel(self) -> None:
        """Explicit cancellation. The outcome is cancelled, never failed."""
        self._edge(
            "cancel",
            AgentRunState.CANCELLED,
            AgentRunState.SUBMITTED,
            AgentRunState.QUEUED,
            AgentRunState.RUNNING,
        )

    def cleanup(self) -> None:
        """Release a terminal run. A live run must be cancelled or failed first.

        Repeating cleanup leaves the terminal state in place. It does not
        relabel cancelled as failed or failed as cancelled.
        """
        if not self._is_terminal():
            self._refuse("cleanup")
        self.cleaned = True
