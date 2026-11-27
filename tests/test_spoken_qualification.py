"""Offline gates and journal correlation for the optional operator-run probe."""
import asyncio
from contextlib import redirect_stdout
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from avi.core.durable_store import Observation, canonical_task
from avi.core.lifecycle import AgentRunState
from avi.core.voice_tool_journal import VoiceCall, VoiceToolJournal


PATH = Path(__file__).parents[1] / "docs/roadmap/delivery/evidence/probe_spoken_delegation.py"
SPEC = spec_from_file_location("spoken_probe", PATH)
probe = module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class SpokenQualificationTests(unittest.TestCase):
    def test_live_flag_and_inputs_gate_all_network_calls(self):
        for args, reason in [([], "live_opt_in_required"), (["--live"], "fixtures_required")]:
            out = io.StringIO()
            with patch.object(probe, "live_runtime", side_effect=AssertionError), redirect_stdout(out):
                self.assertEqual(probe.main(args), 0)
            self.assertEqual(json.loads(out.getvalue())["reason"], reason)

    def test_wave_requires_nonempty_s16le_mono_16khz(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice.wav"
            for rate, data, accept in ((16000, b"\x01\x00", True), (16000, b"\0\0", False), (8000, b"\x01\x00", False)):
                with wave.open(str(path), "wb") as wav:
                    wav.setparams((1, 2, rate, 0, "NONE", ""))
                    wav.writeframes(data)
                if accept:
                    self.assertEqual(probe.speech(path), data)
                else:
                    with self.assertRaises(ValueError):
                        probe.speech(path)

    def test_correlated_observations_ignore_other_sessions_and_paraphrases(self):
        from avi.core.durable_auth import LoopbackCredentials, RunPermission
        from avi.core.durable_runs import DurableRunService
        from avi.core.durable_store import SQLiteRunStore
        from test_jev28_durable_runs import Backend

        query = "synthetic private text"
        digest = sha256(canonical_task("read_only", {"query": query}).encode("ascii")).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            journal = VoiceToolJournal(state / "voice.sqlite3")
            auth = LoopbackCredentials()
            principal = auth.authenticate(auth.issue("operator", RunPermission))
            backend = Backend()
            service = DurableRunService(SQLiteRunStore(state / "runs.sqlite3"), auth, backend,
                                        backend_scope="test", admissions_open=True)

            async def run():
                status = await service.submit(principal, "submit", "read_only", {"query": query})
                backend.observations[status.run_id] = Observation(backend.observations[status.run_id].handle,
                                                                 AgentRunState.COMPLETED, result="sensitive result")
                await service.recover(startup=False)
                return status.run_id

            run_id = asyncio.run(run())
            def bind(session, request):
                call = VoiceCall(session, "turn", "response", request, request)
                journal.begin(call, "synthetic")
                journal.bind_run(call, run_id)
                journal.transition(call, "pending", "acknowledged")
            bind("session", "submit")
            bind("other", "read")
            facts = probe.evidence(state, "session", digest)
            self.assertTrue(facts["backend_completed_with_result"])
            self.assertFalse(facts["result_tool_reply_acknowledged"])
            self.assertEqual(probe.evidence(state, "session", "other digest")["exact_query_submissions"], 0)
            bind("session", "read")
            facts = probe.evidence(state, "session", digest)
            self.assertTrue(facts["result_tool_reply_acknowledged"])
            self.assertEqual(facts["session_submissions"], 1)
            for private in [query, run_id, "sensitive result"]:
                self.assertNotIn(private, json.dumps(facts))

    def test_scripted_voice_session_emits_two_turns_and_waits_for_post_ack_pcm(self):
        class Client:
            remote_address = ("127.0.0.1", 8000)

            def __init__(self):
                self.inbox = asyncio.Queue()
                self.turns = []
                self.controls = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *unused):
                return False

            async def start(self):
                return {"session_id": "session", "capabilities": dict.fromkeys(
                    ("audio_input", "audio_output", "durable_runs"), True,
                )}

            async def send_control(self, message):
                self.controls.append(message["type"])
                self.inbox.put_nowait({"type": "agent.run.authenticated"})

            async def recv(self):
                return await self.inbox.get()

        async def run():
            client = Client()
            report = {"downlink_frames": 0, "followup_pcm_bytes": 0, "pcm_bytes_after_observed_result_ack": 0}

            async def send(_, pcm):
                client.turns.append(pcm)
                if pcm == b"followup":
                    asyncio.get_running_loop().call_later(
                        .4, client.inbox.put_nowait, probe.AudioFrame(probe.AUDIO_DOWNLINK, 0, 0, b"\1\0"),
                    )

            def facts(*unused):
                return {"session_submissions": 1, "backend_completed_with_result": True,
                        "delegation_reply_acknowledged": True, "result_tool_reply_acknowledged": len(client.turns) == 2}

            with patch.object(probe, "AvipClient", return_value=client), patch.object(probe, "send_turn", send), \
                 patch.object(probe, "evidence", facts):
                await asyncio.wait_for(probe.exercise(None, "fixture", "synthetic", b"command", b"followup", report, None), 5)
            self.assertEqual(client.turns, [b"command", b"followup"])
            self.assertEqual(client.controls, ["agent.run.authenticate"])
            self.assertEqual(report["status"], "transport_and_run_evidence_observed")
            self.assertEqual(report["pcm_bytes_after_observed_result_ack"], 2)
        asyncio.run(run())
