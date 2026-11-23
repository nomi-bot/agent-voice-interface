"""Bounded local device events for the simulator and optional ROS bridge."""

from collections.abc import Collection
from dataclasses import dataclass
import json
import re


ACTION_CAPABILITY = "robot_actions"
MAX_MESSAGE_BYTES = 256
_OBSERVATION = "sensor.observation"
_SENSOR_CAPABILITY = "synthetic_sensor"
_ACTION = "record_sample"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
_MAX_ACTIONS = 32
_WORK_SECONDS = 0.05
_TIMEOUT_SECONDS = 0.25


def _identifier(value: object, limit: int) -> bool:
    return type(value) is str and len(value) <= limit and _IDENTIFIER.fullmatch(value) is not None


def _event_fields(message: object) -> tuple[str, str, str, int]:
    if not isinstance(message, dict) or set(message) != {"event_id", "kind", "capability", "payload"}:
        raise ValueError("invalid mock device message")
    event_id, kind, capability, payload = (
        message["event_id"], message["kind"], message["capability"], message["payload"],
    )
    if (
        not _identifier(event_id, 64) or not _identifier(kind, 32) or not _identifier(capability, 32)
        or not isinstance(payload, dict) or set(payload) != {"sample"}
        or type(payload["sample"]) is not int or not 0 <= payload["sample"] <= 100
    ):
        raise ValueError("invalid mock device message")
    return event_id, kind, capability, payload["sample"]


@dataclass(frozen=True)
class ActionRequest:
    event_id: str
    kind: str
    capability: str
    sample: int

    @classmethod
    def parse(cls, message: object) -> "ActionRequest":
        return cls(*_event_fields(message))


@dataclass(frozen=True)
class SensorObservation:
    event_id: str
    sample: int

    @classmethod
    def parse(cls, raw: str) -> "SensorObservation":
        if len(raw) > MAX_MESSAGE_BYTES or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError("oversized mock observation")
        event_id, kind, capability, sample = _event_fields(json.loads(raw))
        if kind != _OBSERVATION or capability != _SENSOR_CAPABILITY:
            raise ValueError("unexpected mock observation")
        return cls(event_id, sample)

    def to_action(self) -> ActionRequest:
        return ActionRequest(self.event_id, _ACTION, ACTION_CAPABILITY, self.sample)


@dataclass(frozen=True)
class DeviceEvent:
    event_id: str
    kind: str
    sample: int | None = None
    reason: str | None = None

    @property
    def payload(self) -> dict:
        if self.sample is not None:
            return {"sample": self.sample}
        if self.reason is not None:
            return {"reason": self.reason}
        return {}

    def message(self) -> dict:
        return {
            "event_id": self.event_id, "kind": self.kind, "capability": ACTION_CAPABILITY,
            "payload": self.payload,
        }


@dataclass
class _Action:
    request: ActionRequest
    started: float
    events: tuple[DeviceEvent, ...]

    @property
    def running(self) -> bool:
        return self.events[-1].kind == "running"


class MockDevice:
    """Run a single allowlisted action that records a sample in memory."""

    def __init__(self, *, capabilities: Collection[str], authorized: bool):
        self._capabilities = frozenset(capabilities)
        self._authorized = authorized
        self._actions: dict[str, _Action] = {}

    @property
    def recorded_samples(self) -> dict[str, int]:
        return {
            event_id: action.request.sample for event_id, action in self._actions.items()
            if action.events[-1].kind == "completed"
        }

    def _permission(self, capability: str) -> str | None:
        if capability != ACTION_CAPABILITY or capability not in self._capabilities:
            return "capability"
        if not self._authorized:
            return "authorization"
        return None

    def submit(self, request: ActionRequest, *, now: float) -> tuple[DeviceEvent, ...]:
        event_id = request.event_id
        reason = self._permission(request.capability)
        if reason is not None:
            return (DeviceEvent(event_id, "denied", reason=reason),)
        if request.kind != _ACTION:
            return (DeviceEvent(event_id, "denied", reason="allowlist"),)
        previous = self._actions.get(event_id)
        if previous is not None:
            if previous.request == request:
                return previous.events
            return (DeviceEvent(event_id, "denied", reason="conflict"),)
        if len(self._actions) == _MAX_ACTIONS:
            return (DeviceEvent(event_id, "denied", reason="capacity"),)
        events = (DeviceEvent(event_id, "accepted"), DeviceEvent(event_id, "running"))
        self._actions[event_id] = _Action(request, now, events)
        return events

    def advance(self, *, now: float) -> tuple[DeviceEvent, ...]:
        updates = []
        for event_id, action in self._actions.items():
            if not action.running:
                continue
            if now >= action.started + _TIMEOUT_SECONDS:
                event = DeviceEvent(event_id, "timed_out")
            elif now >= action.started + _WORK_SECONDS:
                event = DeviceEvent(event_id, "completed", sample=action.request.sample)
            else:
                continue
            action.events += (event,)
            updates.append(event)
        return tuple(updates)

    def cancel(self, event_id: str) -> tuple[DeviceEvent, ...]:
        reason = self._permission(ACTION_CAPABILITY)
        if reason is not None:
            return (DeviceEvent(event_id, "denied", reason=reason),)
        action = self._actions.get(event_id)
        if action is None or not action.running:
            return ()
        event = DeviceEvent(event_id, "cancelled")
        action.events += (event,)
        return (event,)
