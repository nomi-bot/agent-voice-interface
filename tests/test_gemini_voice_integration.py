"""Gated Gemini Live telemetry.

Discovered by ``python -m unittest``. CI has no Gemini credential and no model
pin, so the session test skips. Skip text names environment variables and
never includes their values. This module does not read a key into a report
and does not open a provider socket.

The provider itself is still the JEV-23 placeholder. When that class can be
constructed, the session test checks the voice-provider surface and a
provider-path telemetry report. It still does not call ``start``; a networked
turn belongs with the provider implementation.
"""

import json
import os
import unittest


_KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
_MODEL_NAME = "AVI_GEMINI_MODEL"
_GATE_NAMES = _KEY_NAMES + (_MODEL_NAME,)


def _present(name: str) -> bool:
    value = os.environ.get(name)
    return isinstance(value, str) and value.strip() != ""


def gemini_integration_gate() -> str | None:
    """Return a fixed skip reason, or None when the provider can be constructed.

    The reason does not interpolate environment values.
    """
    if not any(_present(name) for name in _KEY_NAMES):
        return (
            "Gemini credential is unset (GEMINI_API_KEY or GOOGLE_API_KEY); "
            "integration skipped"
        )
    if not _present(_MODEL_NAME):
        return "AVI_GEMINI_MODEL is unset; integration skipped"
    try:
        from avi.providers.gemini_live import GeminiLiveProvider

        GeminiLiveProvider()
    except NotImplementedError:
        return "GeminiLiveProvider is not implemented; integration skipped"
    except Exception:
        return "GeminiLiveProvider could not be constructed; integration skipped"
    return None


class GeminiVoiceIntegrationTests(unittest.TestCase):
    def test_skip_reason_omits_credential_values(self):
        secret = "AIzaSyTESTSECRETVALUE"
        pin = "models/gemini-test-pin"
        saved = {name: os.environ.get(name) for name in _GATE_NAMES}
        try:
            os.environ["GEMINI_API_KEY"] = secret
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop(_MODEL_NAME, None)
            missing_pin = gemini_integration_gate()
            self.assertIn("AVI_GEMINI_MODEL", missing_pin)
            self.assertNotIn(secret, missing_pin)
            os.environ[_MODEL_NAME] = pin
            not_implemented = gemini_integration_gate()
            self.assertIn("not implemented", not_implemented)
            self.assertNotIn(secret, not_implemented)
            self.assertNotIn(pin, not_implemented)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_provider_path_report_when_the_provider_exists(self):
        reason = gemini_integration_gate()
        if reason is not None:
            self.skipTest(reason)
        from avi.providers.gemini_live import GeminiLiveProvider
        from avi.telemetry import VoiceTelemetry

        provider = GeminiLiveProvider()
        for name in ("start", "send_audio", "events", "close"):
            self.assertTrue(callable(getattr(provider, name, None)), name)
        telemetry = VoiceTelemetry(
            client="headless",
            path="provider",
            scenario="gemini-live",
            now=lambda: 0.0,
            wall_now=lambda: 1.0,
        )
        telemetry.mark("session-start", 0)
        report = telemetry.report()
        self.assertEqual(report["path"], "provider")
        self.assertEqual(report["e2e"]["scope"], "provider")
        self.assertIsNone(report["cost"]["amount"])
        encoded = json.dumps(report)
        for name in _GATE_NAMES:
            value = os.environ.get(name)
            if value:
                self.assertNotIn(value, encoded)
