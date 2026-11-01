"""Local voice-session measurements.

The report is a JSON object for a dashboard or a CI artifact. It is not an
AVIP message. Nothing here opens a socket, calls a voice provider, or reads
API keys.

Three clocks appear in every report. They are not interchangeable.

* **Session clock** (``clocks.session``, the browser hook's ``"local"``).
  Monotonic milliseconds on one page or one process. The browser uses
  ``performance.now`` (milliseconds since the page time origin). The gateway
  and this harness use ``time.monotonic`` converted to milliseconds. Deltas
  between marks in one report are valid. Do not subtract this clock from
  another host, from the wall-clock anchor, or from an AVIP media timestamp.
* **Wall clock** (``clocks.wall``). Unix-epoch milliseconds captured once,
  paired with the session clock when the telemetry object is constructed.
  The anchor can place a report on a timeline. Do not subtract wall clocks
  across hosts, and do not compute latency from the wall clock.
* **Media clock** (``clocks.media``). AVIP ``timestamp_us`` is the sender's
  media clock in microseconds. It starts at 0 in each direction. The mock
  mirror's downlink timestamp is the gateway media clock, not the client's.
  Do not subtract a media timestamp from the session clock, from the other
  direction, or across hosts.

End-to-end samples on ``path: "mock-mirror"`` pair the Nth uplink send with
the Nth downlink observation on the session clock. That interval is loopback
through the mock mirror. It is not a model latency and it is not a cross-host
measurement. ``cost`` stays null on this path; a later provider can fill it
with ``note_cost`` without putting a credential in the report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import time
from collections.abc import Mapping
from typing import Callable


SCHEMA = "avi.voice-telemetry/1"
MARKS = (
    "capture-start",
    "session-start",
    "first-uplink",
    "first-downlink",
    "playback-start",
)
COUNTERS = (
    "uplinkFrames",
    "downlinkFrames",
    "playbackFrames",
    "captureDrops",
    "transmitDrops",
    "speakerDrops",
    "timeouts",
    "queueFull",
)
COST_FIELDS = (
    "provider",
    "currency",
    "inputTokens",
    "outputTokens",
    "audioInputSeconds",
    "audioOutputSeconds",
    "amount",
)
COST_STRINGS = frozenset({"provider", "currency"})
BUCKETS_MS = (5, 10, 20, 40, 80, 160, 320, 640, 1280)
SAMPLE_CAP = 4096
NOMINAL_FRAME_MS = 20
MIRROR_DELAY_MS = 5
SPEAKER_DEPTH = 8
UPLINK_HEADER_BYTES = 24
UPLINK_PAYLOAD_BYTES = 640
UPLINK_FRAME_BYTES = UPLINK_HEADER_BYTES + UPLINK_PAYLOAD_BYTES
MAX_UPLINK_QUEUED_BYTES = SPEAKER_DEPTH * UPLINK_FRAME_BYTES
UPLINK_CLOSE_QUEUED_BYTES = MAX_UPLINK_QUEUED_BYTES * 2
MAX_SOAK_SECONDS = 1800
MAX_SOAK_FRAMES = MAX_SOAK_SECONDS * 50
_CLIENTS = frozenset({"browser", "headless", "gateway"})
_PATHS = frozenset({"mock-mirror", "provider"})
_KIND = re.compile(r"^[a-z][a-z0-9-]{0,32}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SESSION_ID = re.compile(r"^s[0-9a-f]{32}$")
_FORBIDDEN_KEY = re.compile(r"(?i)(key|secret|token|password|authorization|credential)")
_SECRET = re.compile(
    r"(?i)(api[_-]?key|secret|bearer\s|AIza[0-9A-Za-z_-]{8,}|sk-[A-Za-z0-9]{8,})"
)
_INTERVALS = (
    ("captureToSession", "capture-start", "session-start"),
    ("captureToUplink", "capture-start", "first-uplink"),
    ("sessionToUplink", "session-start", "first-uplink"),
    ("uplinkToDownlink", "first-uplink", "first-downlink"),
    ("uplinkToAudible", "first-uplink", "playback-start"),
    ("downlinkToAudible", "first-downlink", "playback-start"),
)

SESSION_CLOCK_TEXT = (
    "Monotonic milliseconds on one page or one process. "
    'The browser hook reports this clock as "local" (performance.now, '
    "milliseconds since the page time origin). "
    "The gateway and the headless harness use time.monotonic converted to milliseconds. "
    "Deltas between marks in one report are valid. "
    "Do not subtract this clock from another host, from the wall-clock anchor, "
    "or from an AVIP media timestamp."
)
WALL_CLOCK_TEXT = (
    "Unix-epoch milliseconds captured once, paired with the session clock when "
    "telemetry is constructed. The anchor can place this report on a timeline. "
    "Do not subtract wall clocks across hosts, and do not compute latency from the wall clock."
)
MEDIA_CLOCK_TEXT = (
    "AVIP timestamp_us is the sender's media clock in microseconds. "
    "It starts at 0 in each direction. "
    "The mock mirror's downlink timestamp is the gateway media clock, not the client's. "
    "Do not subtract a media timestamp from the session clock, from the other direction, "
    "or across hosts."
)


def _scrub(value):
    """Replace credential-shaped strings. Structure and numbers stay put."""
    if isinstance(value, str):
        if _SECRET.search(value):
            return "[redacted]"
        return value
    if isinstance(value, dict):
        return {str(key): _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def uplink_queue_decision(
    buffered_amount: object,
    frame_bytes: int = UPLINK_FRAME_BYTES,
) -> str:
    """Match ``uplinkQueueDecision`` in ``clients/browser/voice.js``.

    ``"send"`` queues one frame. ``"drop"`` skips it and leaves the uplink
    sequence where it is. ``"close"`` means the socket has no usable byte budget.
    """
    if (
        isinstance(buffered_amount, bool)
        or not isinstance(buffered_amount, (int, float))
        or not math.isfinite(buffered_amount)
        or buffered_amount < 0
    ):
        return "close"
    if buffered_amount >= UPLINK_CLOSE_QUEUED_BYTES:
        return "close"
    if buffered_amount + frame_bytes > MAX_UPLINK_QUEUED_BYTES:
        return "drop"
    return "send"


def trim_capture_count(count: int, depth: int = SPEAKER_DEPTH) -> tuple[int, int]:
    """Return ``(kept, dropped)`` for one capture callback.

    The browser keeps the newest ``depth`` frames and counts the rest as
    ``captureDrops``. ``depth`` is the 8-frame ``MediaDelivery`` cap.
    """
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError("count must be a non-negative integer")
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
        raise ValueError("depth must be a positive integer")
    if count <= depth:
        return count, 0
    return depth, count - depth


def frames_for_seconds(seconds: float) -> int:
    """Nominal 20 ms frames for a soak, capped at 30 minutes."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("seconds must be a positive finite number")
    if not math.isfinite(seconds) or seconds <= 0 or seconds > MAX_SOAK_SECONDS:
        raise ValueError("seconds must be a positive finite number at most 1800")
    return max(1, int(round(float(seconds) * 50)))


class _Distribution:
    """Exact count, sum, min, max, and histogram.

    Percentiles use every sample until ``sample_cap``. After that they are a
    reservoir estimate and ``approximate`` is true. The histogram, count, min,
    max, and mean stay exact either way. Raw samples are not part of the report.
    """

    def __init__(self, sample_cap: int = SAMPLE_CAP):
        self.sample_cap = sample_cap
        self.count = 0
        self._sum = 0.0
        self.min: float | None = None
        self.max: float | None = None
        self._samples: list[float] = []
        self._approximate = False
        self._histogram = [0] * (len(BUCKETS_MS) + 1)

    def record(self, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        if not math.isfinite(value) or value < 0:
            return
        number = float(value)
        self.count += 1
        self._sum += number
        self.min = number if self.min is None else min(self.min, number)
        self.max = number if self.max is None else max(self.max, number)
        if len(self._samples) < self.sample_cap:
            self._samples.append(number)
        else:
            # Reservoir sample. The index is deterministic for a given count
            # only in the sense that we keep the cap; tests stay under the cap
            # so percentiles there are exact.
            import random

            slot = random.randrange(self.count)
            if slot < self.sample_cap:
                self._samples[slot] = number
            self._approximate = True
        placed = len(BUCKETS_MS)
        for index, edge in enumerate(BUCKETS_MS):
            if number <= edge:
                placed = index
                break
        self._histogram[placed] += 1

    def _percentile(self, fraction: float) -> float | None:
        if not self._samples:
            return None
        ordered = sorted(self._samples)
        rank = math.ceil(fraction * len(ordered)) - 1
        rank = min(max(rank, 0), len(ordered) - 1)
        return ordered[rank]

    def snapshot(self) -> dict:
        histogram = [
            {"leMs": edge, "count": self._histogram[index]}
            for index, edge in enumerate(BUCKETS_MS)
        ]
        histogram.append({"leMs": None, "count": self._histogram[-1]})
        mean = None if self.count == 0 else self._sum / self.count
        return {
            "count": self.count,
            "min": self.min,
            "max": self.max,
            "mean": mean,
            "p50": self._percentile(0.50),
            "p95": self._percentile(0.95),
            "approximate": self._approximate,
            "sampleCap": self.sample_cap,
            "histogram": histogram,
        }


class _FrozenSpeaker:
    """8-frame cap with a clock that does not advance.

    This is the soak stand-in for ``SpeakerBuffer`` when ``now()`` stays put:
    the scheduled horizon is ``depth * 20 ms``, and a new frame past that is
    dropped. Nominal payload is 320 samples at 16 kHz.
    """

    def __init__(self, depth: int = SPEAKER_DEPTH):
        self.depth = depth
        self.next_time = 0.0
        self.played = 0
        self.dropped = 0

    def enqueue(self) -> bool:
        if self.next_time >= self.depth * (320 / 16000):
            self.dropped += 1
            return False
        self.next_time += 320 / 16000
        self.played += 1
        return True

    def depth_frames(self) -> float:
        if self.next_time <= 0:
            return 0.0
        return self.next_time / (320 / 16000)


def _round_frames(value: float) -> float:
    return round(float(value), 6)


class VoiceTelemetry:
    """Counters and timestamps for one voice session.

    ``now`` and ``wall_now`` return milliseconds. Pass them in when a test
    needs a fixed clock. The default session clock is process-monotonic and
    the default wall clock is ``time.time``. Neither is the AVIP media clock.
    """

    def __init__(
        self,
        *,
        client: str,
        path: str = "mock-mirror",
        scenario: str,
        now: Callable[[], float] | None = None,
        wall_now: Callable[[], float] | None = None,
        sample_cap: int = SAMPLE_CAP,
    ):
        self.client = client if client in _CLIENTS else "unknown"
        self.path = path if path in _PATHS else "mock-mirror"
        self.scenario = scenario if isinstance(scenario, str) and _TOKEN.fullmatch(scenario) else "unknown"
        self._now = now if now is not None else (lambda: time.monotonic() * 1000.0)
        self._wall = wall_now if wall_now is not None else (lambda: time.time() * 1000.0)
        self._anchor_session = float(self._now())
        self._anchor_wall = float(self._wall())
        self.marks: dict[str, float] = {}
        self.counters = {name: 0 for name in COUNTERS}
        self.cost = {name: None for name in COST_FIELDS}
        self.timeouts: list[dict] = []
        self.session_id: str | None = None
        self._depth: float | None = None
        self._depth_max: float | None = None
        self._uplink_queued_max: int | None = None
        self._sink_high_water: int | None = None
        self._sink_consumed: int | None = None
        self.e2e = _Distribution(sample_cap)
        self.audible = _Distribution(sample_cap)
        self.jitter = _Distribution(sample_cap)
        self._uplink_at: list[float] = []
        self._pair = 0
        self._last_downlink: float | None = None
        self._last_paired_uplink: float | None = None

    def now(self) -> float:
        return float(self._now())

    def mark(self, name: str, session_ms: float | None = None) -> None:
        """Record the first session-clock time for a known hook. Later calls stick."""
        if name not in MARKS or name in self.marks:
            return
        self.marks[name] = self._at(session_ms)

    def set_session_id(self, value: object) -> None:
        """Keep an opaque gateway id. Any other string is ignored."""
        if isinstance(value, str) and _SESSION_ID.fullmatch(value):
            self.session_id = value

    def add_counter(self, name: str, amount: int = 1) -> None:
        if name not in self.counters or _count(amount) is None:
            return
        self.counters[name] += amount

    def set_counters(self, values: Mapping[str, object]) -> None:
        """Replace named counters. Unknown names and bad values are ignored."""
        if not isinstance(values, Mapping):
            return
        for name in COUNTERS:
            if name not in values:
                continue
            parsed = _count(values[name])
            if parsed is not None:
                self.counters[name] = parsed

    def note_uplink(self, session_ms: float | None = None) -> None:
        self._uplink_at.append(self._at(session_ms))

    def note_downlink(self, session_ms: float | None = None) -> None:
        """Pair this downlink with the next unpaired uplink, if there is one."""
        at = self._at(session_ms)
        if self._last_downlink is not None:
            self.jitter.record(abs(at - self._last_downlink - NOMINAL_FRAME_MS))
        self._last_downlink = at
        if self._pair < len(self._uplink_at):
            sent = self._uplink_at[self._pair]
            self._pair += 1
            self._last_paired_uplink = sent
            delay = at - sent
            if delay >= 0:
                self.e2e.record(delay)
        else:
            self._last_paired_uplink = None
        if self._pair > 64:
            del self._uplink_at[: self._pair]
            self._pair = 0

    def note_audible(self, session_ms: float | None = None) -> None:
        """Session-clock delay from the uplink paired with the latest downlink."""
        if self._last_paired_uplink is None:
            return
        delay = self._at(session_ms) - self._last_paired_uplink
        if delay >= 0:
            self.audible.record(delay)

    def note_buffer_depth(self, frames: object) -> None:
        if isinstance(frames, bool) or not isinstance(frames, (int, float)):
            return
        if not math.isfinite(frames) or frames < 0:
            return
        value = _round_frames(frames)
        self._depth = value
        self._depth_max = value if self._depth_max is None else max(self._depth_max, value)

    def note_uplink_queued(self, buffered_amount: object) -> None:
        if isinstance(buffered_amount, bool) or not isinstance(buffered_amount, int):
            return
        if buffered_amount < 0:
            return
        if self._uplink_queued_max is None or buffered_amount > self._uplink_queued_max:
            self._uplink_queued_max = buffered_amount

    def note_sink(self, high_water: object, consumed: object) -> None:
        if _count(high_water) is not None:
            self._sink_high_water = high_water
        if _count(consumed) is not None:
            self._sink_consumed = consumed

    def note_queue_full(self) -> None:
        self.counters["queueFull"] += 1

    def note_timeout(self, kind: object) -> None:
        """Count a timeout. The stored kind is a short token, or ``other``."""
        token = kind if isinstance(kind, str) and _KIND.fullmatch(kind) else "other"
        self.counters["timeouts"] += 1
        if len(self.timeouts) < 32:
            self.timeouts.append({"kind": token, "sessionMs": self.now()})

    def note_cost(self, fields: Mapping[str, object]) -> None:
        """Merge provider usage. Credential-shaped keys or values are refused.

        A refused update leaves the previous cost fields unchanged. The mock
        path does not call this, so those fields stay null.
        """
        if not isinstance(fields, Mapping):
            return
        for key in fields:
            if key not in COST_FIELDS and _FORBIDDEN_KEY.search(str(key)):
                return
        cleaned: dict[str, object] = {}
        for key in COST_FIELDS:
            if key not in fields:
                continue
            value = fields[key]
            if value is None:
                cleaned[key] = None
                continue
            if key in COST_STRINGS:
                if not isinstance(value, str) or not _TOKEN.fullmatch(value) or _SECRET.search(value):
                    return
                cleaned[key] = value
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                return
            cleaned[key] = value
        self.cost.update(cleaned)

    def report(self) -> dict:
        """Return a JSON-ready snapshot. Strings that look like secrets are redacted."""
        marks = {name: {"sessionMs": self.marks[name]} for name in MARKS if name in self.marks}
        intervals = {}
        for label, start, end in _INTERVALS:
            if start in self.marks and end in self.marks:
                intervals[label] = self.marks[end] - self.marks[start]
            else:
                intervals[label] = None
        e2e_scope = "provider" if self.path == "provider" else "mock-mirror"
        payload = {
            "schema": SCHEMA,
            "path": self.path,
            "client": self.client,
            "scenario": self.scenario,
            "sessionId": self.session_id,
            "clocks": {
                "session": {
                    "name": "local",
                    "unit": "ms",
                    "scope": "page-or-process",
                    "comparableAcrossHosts": False,
                    "description": SESSION_CLOCK_TEXT,
                },
                "wall": {
                    "unit": "ms",
                    "scope": "unix-epoch",
                    "comparableAcrossHosts": False,
                    "anchorSessionMs": self._anchor_session,
                    "anchorWallMs": self._anchor_wall,
                    "description": WALL_CLOCK_TEXT,
                },
                "media": {
                    "unit": "us",
                    "scope": "avip-sender",
                    "comparableAcrossHosts": False,
                    "description": MEDIA_CLOCK_TEXT,
                },
            },
            "marks": marks,
            "intervalsMs": intervals,
            "counters": dict(self.counters),
            "buffer": {
                "depthFrames": self._depth,
                "depthMaxFrames": self._depth_max,
                "uplinkQueuedBytesMax": self._uplink_queued_max,
                "jitter": self.jitter.snapshot(),
                "sinkHighWater": self._sink_high_water,
                "sinkConsumed": self._sink_consumed,
            },
            "e2e": {
                "scope": e2e_scope,
                "unit": "ms",
                "clock": "session",
                "uplinkToDownlink": self.e2e.snapshot(),
                "uplinkToAudible": self.audible.snapshot(),
            },
            "cost": dict(self.cost),
            "timeouts": list(self.timeouts),
        }
        return _scrub(payload)

    def _at(self, session_ms: float | None) -> float:
        if session_ms is None:
            return float(self._now())
        return float(session_ms)


def in_process_soak(frames: int, *, client: str = "headless") -> dict:
    """Drive the mock drop paths on a frozen speaker clock.

    No socket and no microphone. ``frames`` nominal uplinks are each paired
    with a downlink 5 ms later on the session clock. The speaker clock does
    not advance, so frame 9 and later are speaker drops. One 9-frame capture
    burst adds a single ``captureDrops``. Three offers past the uplink byte
    budget add ``transmitDrops``. Cost stays null.
    """
    if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= MAX_SOAK_FRAMES:
        raise ValueError("frames must be an integer from 1 to 90000")
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    telemetry = VoiceTelemetry(
        client=client,
        path="mock-mirror",
        scenario="mock-soak",
        now=now,
        wall_now=lambda: 1_700_000_000_000.0,
    )
    telemetry.mark("capture-start", 0)
    telemetry.mark("session-start", 0)
    speaker = _FrozenSpeaker()
    depth = 0.0
    for index in range(frames):
        uplink_at = 10 + index * NOMINAL_FRAME_MS
        downlink_at = uplink_at + MIRROR_DELAY_MS
        telemetry.note_uplink(uplink_at)
        telemetry.mark("first-uplink", uplink_at)
        telemetry.note_downlink(downlink_at)
        telemetry.mark("first-downlink", downlink_at)
        if speaker.enqueue():
            telemetry.mark("playback-start", downlink_at)
            telemetry.note_audible(downlink_at)
        depth = speaker.depth_frames()
        telemetry.note_buffer_depth(depth)
    _kept, capture_drops = trim_capture_count(9)
    transmit_drops = 0
    for _ in range(3):
        if uplink_queue_decision(MAX_UPLINK_QUEUED_BYTES) == "drop":
            transmit_drops += 1
    telemetry.note_uplink_queued(MAX_UPLINK_QUEUED_BYTES)
    telemetry.set_counters({
        "uplinkFrames": frames,
        "downlinkFrames": frames,
        "playbackFrames": speaker.played,
        "captureDrops": capture_drops,
        "transmitDrops": transmit_drops,
        "speakerDrops": speaker.dropped,
        "timeouts": 0,
        "queueFull": 0,
    })
    return telemetry.report()


async def probe_mock_mirror(url: str, *, frames: int = 2) -> dict:
    """Send silence to a running mock mirror and return a headless report.

    The gateway must already be up with ``AVIP_MOCK_MEDIA=1``. This does not
    read credentials. The URL must not carry user info.
    """
    if not isinstance(url, str) or not (url.startswith("ws://") or url.startswith("wss://")):
        raise ValueError("live probe URL must be ws:// or wss://")
    if "@" in url.split("://", 1)[-1]:
        raise ValueError("live probe URL must not include credentials")
    if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= MAX_SOAK_FRAMES:
        raise ValueError("frames must be an integer from 1 to 90000")
    from avip_client import AvipClient

    telemetry = VoiceTelemetry(client="headless", path="mock-mirror", scenario="mock-probe")
    silence = bytes(UPLINK_PAYLOAD_BYTES)
    async with AvipClient(
        url,
        request_id="req-telemetry-probe",
        device_id="headless-telemetry-001",
        capabilities={
            "audio_input": True,
            "audio_output": True,
            "camera": False,
            "display": False,
            "robot_actions": False,
        },
    ) as client:
        ready = await client.start()
        telemetry.set_session_id(ready.get("session_id"))
        telemetry.mark("session-start")
        for _ in range(frames):
            await client.send_uplink(silence)
            sent = telemetry.now()
            telemetry.note_uplink(sent)
            telemetry.mark("first-uplink", sent)
            frame = await client.recv()
            if type(frame).__name__ != "AudioFrame":
                raise RuntimeError("mock mirror probe expected a downlink frame")
            heard = telemetry.now()
            telemetry.note_downlink(heard)
            telemetry.mark("first-downlink", heard)
        telemetry.set_counters({
            "uplinkFrames": frames,
            "downlinkFrames": frames,
            "playbackFrames": 0,
            "captureDrops": 0,
            "transmitDrops": 0,
            "speakerDrops": 0,
        })
    return telemetry.report()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write a mock-mirror voice telemetry report as JSON. "
            "The in-process soak does not open a microphone or a provider. "
            "--live expects AVIP_MOCK_MEDIA=1 and does not read API keys."
        )
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=2.0,
        help="Nominal-frame duration of the in-process soak (20 ms frames, max 1800)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=None,
        help="Frame count instead of --seconds (1 to 90000)",
    )
    parser.add_argument(
        "--live",
        default=None,
        help="Also probe ws://127.0.0.1:8000/ws/avip (or another mock mirror)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    frames = args.frames if args.frames is not None else frames_for_seconds(args.seconds)
    if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= MAX_SOAK_FRAMES:
        raise SystemExit("frames must be an integer from 1 to 90000")
    report = in_process_soak(frames)
    if args.live:
        try:
            live = asyncio.run(probe_mock_mirror(args.live, frames=frames))
        except Exception as exc:
            message = str(exc)
            if _SECRET.search(message):
                message = "[redacted]"
            raise SystemExit(f"mock mirror probe failed: {message}") from None
        payload: object = {"inProcess": report, "live": live}
    else:
        payload = report
    json.dump(payload, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
