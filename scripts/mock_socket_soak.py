"""Qualify the local AVIP PCM mirror over real TCP sockets (Linux/macOS)."""
import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import resource
import shlex
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

from avip_client.framing import AUDIO_DOWNLINK, NOMINAL_CHUNK_US, NOMINAL_PAYLOAD_BYTES
from avip_client.session import AvipClient


ROOT = Path(__file__).resolve().parents[1]
FRAME_SECONDS = NOMINAL_CHUNK_US / 1_000_000


@contextmanager
def gateway():
    """Reserve a loopback port and pass its fd to a dedicated mock-only process."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        command = [sys.executable, "-m", "uvicorn", "avi.main:app", "--fd",
                   str(listener.fileno()), "--log-level", "warning", "--no-access-log",
                   "--ws-max-size", "16384", "--ws-max-queue", "16"]
        env = {"PATH": os.defpath, "AVIP_MOCK_MEDIA": "1", "PYTHONPATH": str(ROOT / "src")}
        proc = subprocess.Popen(command, env=env, cwd=ROOT, pass_fds=(listener.fileno(),))
        try:
            opener = build_opener(ProxyHandler({}))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError("gateway exited during startup")
                try:
                    with opener.open(f"http://127.0.0.1:{port}/health", timeout=0.3) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.05)
            else:
                raise TimeoutError("gateway startup")
            yield f"ws://127.0.0.1:{port}/ws/avip"
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@dataclass
class Counts:
    connections: int = 0
    sent_frames: int = 0
    verified_frames: int = 0
    error: str | None = None


async def stream(url, number, counts, end, reconnect_seconds):
    loop = asyncio.get_running_loop()
    try:
        while loop.time() < end:
            client = AvipClient(url, request_id=f"q-{number}-{counts.connections}",
                                device_id=f"headless-{number}",
                                capabilities={"audio_input": True, "audio_output": True})
            async with client:
                async with asyncio.timeout(5):
                    ready = await client.start()
                if not all(ready["capabilities"].get(key) for key in ("audio_input", "audio_output")):
                    raise ValueError("missing mirror capabilities")
                counts.connections += 1
                session_end = min(end, loop.time() + reconnect_seconds)
                sequence = 0
                due = loop.time()
                while due < session_end:
                    await asyncio.sleep(max(0, due - loop.time()))
                    # Keep distinct data per frame and per client so replay/cross-talk fails.
                    sample = counts.sent_frames.to_bytes(8, "little") + number.to_bytes(8, "little")
                    pcm = sample * (NOMINAL_PAYLOAD_BYTES // len(sample))
                    async with asyncio.timeout(5):
                        await client.send_uplink(pcm)
                        counts.sent_frames += 1
                        frame = await client.recv()
                    if (frame.direction, frame.sequence, frame.timestamp_us, frame.payload) != (
                        AUDIO_DOWNLINK, sequence, sequence * NOMINAL_CHUNK_US, pcm
                    ):
                        raise ValueError("mirror mismatch")
                    counts.verified_frames += 1
                    sequence += 1
                    due = max(due + FRAME_SECONDS, loop.time())
                await asyncio.sleep(max(0, session_end - loop.time()))
        if not counts.verified_frames:
            raise RuntimeError("no media samples")
    except Exception as exc:
        counts.error = type(exc).__name__


async def exercise(url, clients, seconds, reconnect_seconds):
    end = asyncio.get_running_loop().time() + seconds
    counts = [Counts() for _ in range(clients)]
    await asyncio.gather(*(stream(url, i, item, end, reconnect_seconds)
                           for i, item in enumerate(counts)))
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=1800)
    parser.add_argument("--clients", type=int, default=2)
    parser.add_argument("--reconnect-seconds", type=float, default=60)
    args = parser.parse_args()
    if not all(math.isfinite(n) and n > 0 for n in (args.seconds, args.reconnect_seconds)) or args.clients < 1:
        parser.error("durations and client count must be positive and finite")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
    started = datetime.now(timezone.utc)
    error = None
    counts = []
    with_start = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        with gateway() as url:
            counts = asyncio.run(exercise(url, args.clients, args.seconds, args.reconnect_seconds))
    except Exception as exc:
        error = type(exc).__name__
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    elapsed = time.monotonic() - with_start
    failures = int(error is not None) + sum(c.error is not None for c in counts)
    report = {
        "scenario": "headless Python SDK / loopback TCP / mock PCM mirror",
        "command": "python " + shlex.join(["scripts/mock_socket_soak.py", *sys.argv[1:]]),
        "revision": revision, "dirty_checkout": dirty, "platform": platform.platform(),
        "python": platform.python_version(),
        "dependencies": {p: version(p) for p in ("fastapi", "uvicorn", "websockets")},
        "started": started.isoformat(), "ended": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": elapsed, "clock": "monotonic", "settings": vars(args),
        "clients": [asdict(c) for c in counts], "gateway_error": error,
        "unrecoverable_failures": failures, "passed": failures == 0,
        "dropped_or_unverified_frames": sum(c.sent_frames - c.verified_frames for c in counts),
        "planned_reconnects_completed": sum(max(0, c.connections - 1) for c in counts),
        "gateway_cpu_seconds": after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
        "child_peak_rss_bytes": after.ru_maxrss * (1 if sys.platform == "darwin" else 1024),
        "rss_scope": "maximum over gateway and short git metadata subprocesses",
        "conversation_turns": 0, "voice_interruptions": 0, "agent_tasks": 0,
        "provider_usage": None, "cost": None,
    }
    print(json.dumps(report, indent=2))
    return int(failures != 0)


if __name__ == "__main__":
    sys.exit(main())
