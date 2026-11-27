"""Finite mock microphone and speaker session on the shared AVIP gateway."""

import argparse
import asyncio
from collections import deque
from dataclasses import asdict, dataclass
import json
import sys

from websockets.exceptions import WebSocketException

from avip_client.errors import AvipError
from avip_client.framing import AUDIO_DOWNLINK, NOMINAL_CHUNK_US, NOMINAL_PAYLOAD_BYTES, AudioFrame
from avip_client.messages import PROTOCOL
from avip_client.session import AvipClient


_SCENARIOS = ("normal", "slow-speaker", "disconnect", "stop-playback", "stream")
_AUDIO = {"audio_input": True, "audio_output": True}
_FRAME_SECONDS = NOMINAL_CHUNK_US / 1_000_000
_SPEAKER_DEPTH = 8
_MAX_FRAMES = 3_000


def mock_pcm(frame_index: int) -> bytes:
    """Produce 20 ms of deterministic 16 kHz, mono, signed little-endian PCM."""
    samples_per_frame = NOMINAL_PAYLOAD_BYTES // 2
    start = frame_index * samples_per_frame
    return b"".join(
        ((sample % 97 - 48) * 512).to_bytes(2, "little", signed=True)
        for sample in range(start, start + samples_per_frame)
    )


class Speaker:
    """Bounded fake playback with independent local stop and server interruptions."""

    def __init__(self):
        self._frames: deque[AudioFrame] = deque()
        self._generation = 0
        self._stopped = False
        self.played = 0
        self.dropped = 0
        self.flushed = 0
        self.interruptions = 0
        self.peak = 0

    def enqueue(self, frame: AudioFrame) -> bool:
        if self._stopped or len(self._frames) == _SPEAKER_DEPTH:
            self.dropped += 1
            return False
        self._frames.append(frame)
        self.peak = max(self.peak, len(self._frames))
        return True

    def play_next(self) -> AudioFrame | None:
        if not self._frames:
            return None
        self.played += 1
        return self._frames.popleft()

    def interrupt(self, event: dict) -> None:
        generation = event.get("generation")
        if (
            event.get("protocol") == PROTOCOL
            and event.get("type") == "response.interrupted"
            and type(generation) is int
            and generation == self._generation
        ):
            self._flush()
            self._generation += 1
            self.interruptions += 1

    def stop(self) -> None:
        self._stopped = True
        self._flush()

    def new_session(self) -> None:
        self._flush()
        self._generation = 0
        self._stopped = False

    def _flush(self) -> None:
        self.flushed += len(self._frames)
        self._frames.clear()


@dataclass(frozen=True)
class SimulationReport:
    sessions: int
    uplink_frames: int
    downlink_frames: int
    played_frames: int
    dropped_frames: int
    flushed_frames: int
    peak_queue_frames: int
    capabilities: dict[str, bool]
    interruptions: int


def _session_lengths(frames: int, scenario: str) -> tuple[int, ...]:
    if type(frames) is not int or not 1 <= frames <= _MAX_FRAMES:
        raise ValueError("frames out of range")
    if scenario not in _SCENARIOS:
        raise ValueError("unknown simulator scenario")
    if scenario in {"disconnect", "stop-playback"} and frames < 2:
        raise ValueError("scenario requires at least two frames")
    if scenario == "disconnect":
        return (frames // 2, frames - frames // 2)
    return (frames,)


async def _receive_frame(client: AvipClient, speaker: Speaker) -> AudioFrame:
    while True:
        message = await client.recv()
        if isinstance(message, AudioFrame):
            return message
        speaker.interrupt(message)


async def _stream(client: AvipClient, speaker: Speaker, frames: int) -> int:
    """Drive a finite microphone and accept independent provider output."""
    received = 0

    async def receive():
        nonlocal received
        while True:
            message = await client.recv()
            if isinstance(message, AudioFrame):
                if message.direction != AUDIO_DOWNLINK:
                    raise AvipError("invalid_downlink")
                received += 1
                speaker.enqueue(message)
            else:
                speaker.interrupt(message)

    async def capture_and_play():
        for index in range(frames):
            await client.send_uplink(mock_pcm(index))
            await asyncio.sleep(_FRAME_SECONDS)
            speaker.play_next()
        # Keep listening for a bounded half second after the microphone ends.
        for _ in range(25):
            await asyncio.sleep(_FRAME_SECONDS)
            speaker.play_next()

    reader = asyncio.create_task(receive())
    capture = asyncio.create_task(capture_and_play())
    try:
        finished, _ = await asyncio.wait((reader, capture), return_when=asyncio.FIRST_COMPLETED)
        for task in finished:
            task.result()
    finally:
        for task in (reader, capture):
            task.cancel()
        await asyncio.gather(reader, capture, return_exceptions=True)
    return received


async def run_simulation(
    url: str,
    *,
    frames: int = 24,
    scenario: str = "normal",
    origin: str = "http://127.0.0.1:8000",
) -> SimulationReport:
    """Drive a finite paced microphone with a mirror or independent streaming provider."""
    sessions = _session_lengths(frames, scenario)
    speaker = Speaker()
    sent = 0
    received = 0
    capabilities = {}
    async with asyncio.timeout(5 + frames * 0.04):
        for session, frame_count in enumerate(sessions):
            speaker.new_session()
            async with AvipClient(
                url, request_id=f"simulator-{session}", device_id="headless-simulator",
                capabilities=_AUDIO, origin=origin,
            ) as client:
                ready = await client.start()
                capabilities = ready.get("capabilities")
                if not isinstance(capabilities, dict) or any(
                    capabilities.get(capability) is not True for capability in _AUDIO
                ):
                    raise AvipError("audio_not_negotiated")
                if scenario == "stream":
                    received = await _stream(client, speaker, frame_count)
                    sent = frame_count
                else:
                    for sequence in range(frame_count):
                        if scenario == "stop-playback" and sequence == frames // 2:
                            speaker.stop()
                        payload = mock_pcm(sent)
                        await client.send_uplink(payload)
                        sent += 1
                        downlink = await _receive_frame(client, speaker)
                        if downlink != AudioFrame(AUDIO_DOWNLINK, sequence, sequence * NOMINAL_CHUNK_US, payload):
                            raise AvipError("mock_downlink_mismatch")
                        received += 1
                        speaker.enqueue(downlink)
                        if scenario != "slow-speaker" or received % 4 == 0:
                            speaker.play_next()
                        await asyncio.sleep(_FRAME_SECONDS)
            while speaker.play_next() is not None:
                pass
    return SimulationReport(
        sessions=len(sessions), uplink_frames=sent, downlink_frames=received,
        played_frames=speaker.played, dropped_frames=speaker.dropped,
        flushed_frames=speaker.flushed, peak_queue_frames=speaker.peak, capabilities=capabilities,
        interruptions=speaker.interruptions,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a finite AVIP audio simulation.")
    parser.add_argument("url", help="Gateway WebSocket URL")
    parser.add_argument("--origin", default="http://127.0.0.1:8000")
    parser.add_argument("--frames", type=int, default=24)
    parser.add_argument("--scenario", choices=_SCENARIOS, default="normal")
    args = parser.parse_args(argv)
    try:
        _session_lengths(args.frames, args.scenario)
    except ValueError as error:
        parser.error(str(error))
    try:
        report = asyncio.run(run_simulation(
            args.url, frames=args.frames, scenario=args.scenario, origin=args.origin,
        ))
    except AvipError as error:
        message = error.code if error.code in {"audio_not_negotiated", "mock_downlink_mismatch", "invalid_downlink"} else "gateway_error"
        print(message, file=sys.stderr)
        return 1
    except (OSError, TimeoutError, WebSocketException, ValueError):
        print("gateway_unavailable", file=sys.stderr)
        return 1
    print(json.dumps(asdict(report), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
