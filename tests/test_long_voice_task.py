"""Real loopback sockets with a scripted audio provider and monotonic-clock backend."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import uvicorn

from avi.core.durable_auth import RunPermission
from avi.core.durable_store import Observation
from avi.core.events import VoiceEvent
from avi.core.lifecycle import AgentRunState
from avi.core.voice_delegation import DELEGATE_TOOL, RESULT_TOOL
from avi.local_gateway import open_runtime
from avi.main import app
from avi.protocols.avip import Capabilities
from avi.providers.mount import SessionVoice, resolve_voice_mount
from avip_client.errors import AvipError
from avip_client.session import AvipClient
from avip_client.simulator import Speaker, mock_pcm
from test_avip_live_mount import FakeProvider


QUERY = "synthetic lookup"
ANSWER = "synthetic answer"


class TimedBackend:
    cancellation = True

    def __init__(self, seconds):
        self.seconds = seconds
        self.starts = {}
        self.ends = {}
        self.cancelled = []
        self.submissions = []

    async def submit(self, owner, *, kind, arguments, correlation_id):
        self.submissions.append(correlation_id)
        self.starts[correlation_id] = time.monotonic()
        return Observation(correlation_id, AgentRunState.RUNNING, progress="started")

    async def reconcile(self, owner, *, correlation_id, handle):
        if time.monotonic() - self.starts[correlation_id] < self.seconds:
            return Observation(handle, AgentRunState.RUNNING, progress="working")
        self.ends[correlation_id] = time.monotonic()
        return Observation(handle, AgentRunState.COMPLETED, result=ANSWER)

    async def cancel(self, owner, handle):
        self.cancelled.append(handle)
        return Observation(handle, AgentRunState.CANCELLED)

    async def close(self):
        pass


class ScriptedSpeech(FakeProvider):
    """Synthetic PCM markers select scripted speech intents. No external calls."""

    def enable_delegation_tools(self):
        self.enabled = True

    async def send_audio(self, pcm):
        if pcm == mock_pcm(0):
            await self.emit(VoiceEvent("tool_call", {"id": "submit", "name": DELEGATE_TOOL, "args": {"query": QUERY}}))
        elif pcm == mock_pcm(1):
            await self.emit(VoiceEvent("interrupted"))
        elif pcm == mock_pcm(2):
            await self.emit(VoiceEvent("tool_call", {"id": "read", "name": RESULT_TOOL, "args": {"run_id": self.result_run_id}}))

    async def tool_result(self, call_id, result):
        await super().tool_result(call_id, result)
        if call_id == "submit" and result.get("output", {}).get("status") == "running":
            await self.emit(VoiceEvent("audio", mock_pcm(3)))
        elif call_id == "read" and result.get("output", {}).get("result") == ANSWER:
            await self.emit(VoiceEvent("audio", mock_pcm(4)))


async def request(client, operation, **fields):
    await client.send_control({"protocol": "avip/1", "type": f"agent.run.{operation}", **fields})
    return await client.recv()


class LongVoiceTaskQualification(unittest.IsolatedAsyncioTestCase):
    async def test_voice_task_across_interruption_and_reconnect(self):
        long = os.environ.get("AVI_TEST_LONG_TASK") == "1"
        backend = TimedBackend(61 if long else 2)
        providers = []

        def mount(_):
            provider = ScriptedSpeech()
            providers.append(provider)
            return SessionVoice(provider, Capabilities(audio_input=True, audio_output=True),
                                "provider", "scripted-speech", resolve_voice_mount({}))

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runtime = await self.enterAsyncContext(open_runtime(
            directory, backend=backend, owner="operator", scope="controlled-local",
            queries=[QUERY], read_only=False, grant_seconds=180,
        ))
        self.enterContext(patch.object(app.state, "durable_runtime", runtime, create=True))
        self.enterContext(patch("avi.main.attach_voice", side_effect=mount))
        listener = self.enterContext(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error", proxy_headers=False))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))

        async def shutdown():
            server.should_exit = True
            await asyncio.wait_for(server_task, 5)

        self.addAsyncCleanup(shutdown)
        async with asyncio.timeout(5):
            while not server.started:
                if server_task.done():
                    server_task.result()
                    self.fail("gateway did not start")
                await asyncio.sleep(0.01)
        token = (directory / "client.grant").read_text().strip()
        sessions = []

        @asynccontextmanager
        async def session(grant, *, audio):
            caps = {"durable_runs": True, "durable_run_discovery": True, "audio_input": audio, "audio_output": audio}
            async with AvipClient(f"ws://127.0.0.1:{port}/ws/avip", request_id="connect",
                                  device_id="sim", capabilities=caps) as client:
                ready = await client.start()
                self.assertTrue(ready["capabilities"]["durable_run_discovery"])
                sessions.append(ready["session_id"])
                reply = await request(client, "authenticate", token=grant)
                self.assertEqual(reply["type"], "agent.run.authenticated")
                yield client

        speaker = Speaker()
        started = datetime.now(timezone.utc).isoformat()
        async with asyncio.timeout(backend.seconds + 15):
            async with session(token, audio=True) as first:
                await first.send_uplink(mock_pcm(0))
                first_audio = await first.recv()
                self.assertEqual(first_audio.payload, mock_pcm(3))
                self.assertTrue(speaker.enqueue(first_audio))
                discovered = (await request(first, "recent"))["runs"]
                self.assertEqual(len(discovered), 1)
                run_id = discovered[0]["run_id"]
                before = await request(first, "status", run_id=run_id)
                self.assertEqual(before["state"], "running")
                await first.send_uplink(mock_pcm(1))
                speaker.interrupt(await first.recv())
                self.assertEqual((speaker.interruptions, speaker.flushed), (1, 1))
            foreign = runtime.credentials.issue("other", RunPermission)
            async with session(foreign, audio=False) as stranger:
                with self.assertRaises(AvipError) as denied:
                    await request(stranger, "result", run_id=run_id)
                self.assertEqual(denied.exception.code, "access_denied")
            async with session(token, audio=True) as resumed:
                self.assertEqual((await request(resumed, "recent"))["runs"], discovered)
                polls = 0
                while True:
                    status = await request(resumed, "status", run_id=run_id)
                    polls += 1
                    if status["state"] == "completed":
                        break
                    self.assertEqual(status["state"], "running")
                    await asyncio.sleep(2 if long else 0.2)
                events = []
                event = await request(resumed, "attach", run_id=run_id, after_cursor=before["cursor"])
                while event["type"] == "agent.run.event":
                    events.append(event)
                    event = await resumed.recv()
                cursors = [entry["cursor"] for entry in events]
                self.assertEqual(cursors, list(range(before["cursor"] + 1, status["cursor"] + 1)))
                self.assertEqual(event["type"], "agent.run.replay")
                self.assertEqual(event["resume_cursor"], status["cursor"])
                self.assertIn("progress", [entry["kind"] for entry in events])
                self.assertEqual([entry["payload"]["result"] for entry in events if entry["kind"] == "completed"], [ANSWER])
                repeated = await request(resumed, "attach", run_id=run_id, after_cursor=event["resume_cursor"])
                self.assertEqual(repeated, event)
                self.assertEqual((await request(resumed, "result", run_id=run_id))["result"], ANSWER)
                providers[-1].result_run_id = run_id
                await resumed.send_uplink(mock_pcm(2))
                speaker.new_session()
                self.assertTrue(speaker.enqueue(await resumed.recv()))
                self.assertEqual(speaker.play_next().payload, mock_pcm(4))
                self.assertEqual(backend.submissions, [run_id])
                self.assertFalse(backend.cancelled)
                second = await request(resumed, "submit", request_id="cancel-example", kind="read_only",
                                       arguments={"query": QUERY})
                self.assertNotEqual(second["run_id"], run_id)
                cancellation = await request(resumed, "cancel", run_id=second["run_id"])
                self.assertEqual(cancellation["state"], "cancelled")
                self.assertEqual(backend.cancelled, [second["run_id"]])
                self.assertEqual(backend.submissions, [run_id, second["run_id"]])
            self.assertEqual(len(set(sessions)), 3)
        elapsed = backend.ends[run_id] - backend.starts[run_id]
        self.assertGreaterEqual(elapsed, backend.seconds)
        if long:
            print(json.dumps({
                "kind": "real-loopback-websocket/scripted-provider/timed-backend", "result": "passed",
                "sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parent, text=True).strip(),
                "started": started, "finished": datetime.now(timezone.utc).isoformat(),
                "python": platform.python_version(), "uvicorn": version("uvicorn"), "websockets": version("websockets"),
                "clock": "backend process monotonic", "task_runtime_seconds": round(elapsed, 3),
                "websocket_sessions": len(sessions), "foreign_owner_denials": 1, "status_polls_after_reconnect": polls,
                "interruptions": speaker.interruptions, "flushed_synthetic_frames": speaker.flushed,
                "replayed_events": len(events), "completed_results": 1, "result_audio_frames_played": speaker.played,
                "primary_dispatches": backend.submissions.count(run_id),
                "separate_confirmed_cancellations": len(backend.cancelled),
                "remote_provider_calls": 0,
            }, sort_keys=True))
