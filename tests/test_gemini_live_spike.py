import asyncio
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import re
import unittest

from avi.providers.gemini_live import GeminiLiveProvider
from avi.providers.gemini_live_spike import (
    INPUT_SAMPLE_RATE_HZ,
    OUTPUT_SAMPLE_RATE_HZ,
    SDK_PIN,
    ModelUnconfigured,
    SmokeStatus,
    main,
    pcm_input_blob,
    plan_smoke,
    read_signal,
    redact,
    validate_pcm_s16le,
)


_MODEL_LITERAL = re.compile(r"gemini-\d")


class GeminiLiveSpikeTests(unittest.TestCase):
    def test_blank_key_skips_even_when_a_model_is_present(self):
        plan = plan_smoke(
            {"GEMINI_API_KEY": "  ", "GEMINI_LIVE_MODEL": "operator-selected-model"}
        )
        self.assertEqual(plan.status, SmokeStatus.SKIPPED)
        self.assertIsNone(plan.model)
        self.assertIsNone(plan.api_key)

    def test_key_without_model_fails_closed(self):
        with self.assertRaises(ModelUnconfigured):
            plan_smoke({"GEMINI_API_KEY": "present", "GEMINI_LIVE_MODEL": "   "})

    def test_resource_paths_are_rejected(self):
        with self.assertRaises(ModelUnconfigured):
            plan_smoke(
                {
                    "GEMINI_API_KEY": "present",
                    "GEMINI_LIVE_MODEL": "models/operator-selected-model",
                }
            )

    def test_plan_uses_the_operator_model_and_hides_the_key(self):
        secret = "super-secret-value"
        plan = plan_smoke(
            {"GEMINI_API_KEY": secret, "GEMINI_LIVE_MODEL": "operator-selected-model"}
        )
        self.assertEqual(plan.status, SmokeStatus.READY)
        self.assertEqual(plan.model, "operator-selected-model")
        rendered = repr(plan)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(secret, str(plan.api_key))
        self.assertEqual(plan.api_key.value, secret)

    def test_redact_removes_the_key_from_a_single_line(self):
        self.assertEqual(
            redact("ClientError: key super-secret-value rejected", "super-secret-value"),
            "ClientError: key [redacted] rejected",
        )

    def test_pcm_input_contract(self):
        blob = pcm_input_blob(data=b"\x00\x00\x01\x00")
        self.assertEqual(blob["mime_type"], f"audio/pcm;rate={INPUT_SAMPLE_RATE_HZ}")
        self.assertEqual(OUTPUT_SAMPLE_RATE_HZ, 24_000)
        with self.assertRaises(ValueError):
            validate_pcm_s16le(b"\x00")
        with self.assertRaises(ValueError):
            validate_pcm_s16le(b"")

    def test_provider_remains_unimplemented(self):
        with self.assertRaises(NotImplementedError):
            GeminiLiveProvider()

    def test_python_sources_do_not_embed_model_ids(self):
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for base in (root / "src", root / "tests"):
            for path in base.rglob("*.py"):
                if _MODEL_LITERAL.search(path.read_text()):
                    offenders.append(str(path.relative_to(root)))
        self.assertEqual(offenders, [])

    def test_google_api_key_alone_does_not_authorize_a_live_call(self):
        plan = plan_smoke(
            {
                "GOOGLE_API_KEY": "other-secret",
                "GEMINI_LIVE_MODEL": "operator-selected-model",
            }
        )
        self.assertEqual(plan.status, SmokeStatus.SKIPPED)
        self.assertNotIn("other-secret", repr(plan))

    def test_invalid_model_is_rejected_without_echoing_the_value(self):
        secret = "pasted-secret"
        with self.assertRaises(ModelUnconfigured) as caught:
            plan_smoke(
                {"GEMINI_API_KEY": "present", "GEMINI_LIVE_MODEL": secret + "/models/path"}
            )
        self.assertNotIn(secret, str(caught.exception))

    def test_cli_fail_closed_without_a_key(self):
        buffer = StringIO()
        with redirect_stderr(buffer):
            code = main(env={})
        self.assertEqual(code, 2, buffer.getvalue())
        self.assertIn("status=skipped", buffer.getvalue())

    def test_cli_fail_closed_without_a_model(self):
        buffer = StringIO()
        with redirect_stderr(buffer):
            code = main(env={"GEMINI_API_KEY": "present"})
        self.assertEqual(code, 3, buffer.getvalue())
        self.assertNotIn("present", buffer.getvalue())

    def test_sdk_pin_symbols_and_server_signals(self):
        from google.genai import types
        from google.genai.live import AsyncSession

        from avi.providers.gemini_live_spike import assert_sdk_pin, native_audio_config

        self.assertEqual(assert_sdk_pin(), SDK_PIN)
        self.assertEqual(
            tuple(member.value for member in types.Behavior),
            ("UNSPECIFIED", "BLOCKING", "NON_BLOCKING"),
        )
        self.assertEqual(
            tuple(member.value for member in types.FunctionResponseScheduling),
            ("SCHEDULING_UNSPECIFIED", "SILENT", "WHEN_IDLE", "INTERRUPT"),
        )
        self.assertEqual(
            tuple(member.value for member in types.ActivityHandling),
            (
                "ACTIVITY_HANDLING_UNSPECIFIED",
                "START_OF_ACTIVITY_INTERRUPTS",
                "NO_INTERRUPTION",
            ),
        )
        for name in (
            "send_client_content",
            "send_realtime_input",
            "send_tool_response",
            "receive",
            "close",
        ):
            self.assertTrue(callable(getattr(AsyncSession, name)))
        module_source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "avi"
            / "providers"
            / "gemini_live_spike.py"
        ).read_text()
        self.assertNotIn("start_stream", module_source)
        self.assertIn("vertexai=False", module_source)
        self.assertNotIn(".send(", module_source)

        config = native_audio_config()
        dumped = config.model_dump(exclude_none=True)
        self.assertEqual(dumped["response_modalities"], [types.Modality.AUDIO])
        self.assertIn("session_resumption", dumped)
        self.assertNotIn("thinking_config", dumped)
        self.assertNotIn("enable_affective_dialog", dumped)
        self.assertNotIn("proactivity", dumped)

        cancelled = types.LiveServerMessage(
            tool_call_cancellation=types.LiveServerToolCallCancellation(ids=["call-1"])
        )
        signal = read_signal(cancelled)
        self.assertEqual(signal.kind, "tool_call_cancellation")
        self.assertEqual(signal.cancellation_ids, ("call-1",))

        interrupted = types.LiveServerMessage(
            server_content=types.LiveServerContent(
                interrupted=True,
                turn_complete=True,
                model_turn=types.Content(
                    parts=[types.Part(inline_data=types.Blob(data=b"\x00\x00", mime_type="audio/pcm"))]
                ),
            )
        )
        signal = read_signal(interrupted)
        self.assertEqual(signal.kind, "interrupted")
        self.assertTrue(signal.turn_complete)
        self.assertEqual(signal.audio_bytes, 2)

        away = types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="10s"))
        self.assertEqual(read_signal(away).kind, "go_away")

        resumed = types.LiveServerMessage(
            session_resumption_update=types.LiveServerSessionResumptionUpdate(
                resumable=True,
                new_handle="handle-secret",
            )
        )
        signal = read_signal(resumed)
        self.assertTrue(signal.resumption_handle_present)
        self.assertNotIn("handle-secret", repr(signal))

    def test_live_round_trip_when_configured(self):
        import os

        from avi.providers.gemini_live_spike import run_live_smoke

        try:
            plan = plan_smoke(os.environ)
        except ModelUnconfigured as exc:
            self.fail(str(exc))
        if plan.status == SmokeStatus.SKIPPED:
            self.skipTest("GEMINI_API_KEY unset")
        report = asyncio.run(run_live_smoke(os.environ))
        self.assertEqual(report.status, "passed", report.reason)
        self.assertGreater(report.audio_bytes + int(report.saw_transcript), 0)


if __name__ == "__main__":
    unittest.main()
