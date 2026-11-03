"""Gated Gemini Live telemetry.

Discovered by ``python -m unittest``. The operator contract matches ADR-011
and ``avi.providers.gemini_live_spike``: ``GEMINI_API_KEY`` is the only
credential, and ``GEMINI_LIVE_MODEL`` is the only model pin. ``GOOGLE_API_KEY``
is ignored. There is no ``AVI_GEMINI_MODEL`` alias.

CI sets neither variable, so the session test skips. Skip and failure text
name the variables and never include their values. This module does not read
a key into a report and does not open a provider socket.

``GeminiLiveProvider`` can be constructed. The session test checks that
surface and a provider-path telemetry report. It does not call ``start``.
"""

import json
import os
import unittest


_KEY_NAME = "GEMINI_API_KEY"
_MODEL_NAME = "GEMINI_LIVE_MODEL"
_IGNORED = ("GOOGLE_API_KEY", "AVI_GEMINI_MODEL")
_SAVED = (_KEY_NAME, _MODEL_NAME, *_IGNORED)


class GeminiModelUnconfigured(Exception):
    """A key is set and the model pin is not. The message has no secret."""


def _present(name: str) -> bool:
    value = os.environ.get(name)
    return isinstance(value, str) and value.strip() != ""


def gemini_integration_gate() -> str | None:
    """Return a fixed skip reason, or None when the provider can be constructed.

    The reason does not interpolate environment values. A set key with no
    ``GEMINI_LIVE_MODEL`` raises ``GeminiModelUnconfigured`` instead of skipping.
    """
    if not _present(_KEY_NAME):
        return "GEMINI_API_KEY is unset; integration skipped"
    if not _present(_MODEL_NAME):
        raise GeminiModelUnconfigured(
            "GEMINI_API_KEY is set but GEMINI_LIVE_MODEL is unset; "
            "refusing to choose a model"
        )
    try:
        from avi.providers.gemini_live import GeminiLiveProvider

        GeminiLiveProvider()
    except NotImplementedError:
        return "GeminiLiveProvider is not implemented; integration skipped"
    except Exception:
        return "GeminiLiveProvider could not be constructed; integration skipped"
    return None


class GeminiVoiceIntegrationTests(unittest.TestCase):
    def test_gate_matches_adr_011_and_omits_credential_values(self):
        secret = "AIzaSyTESTSECRETVALUE"
        pin = "gemini-test-pin"
        alias = "models/avi-gemini-alias"
        saved = {name: os.environ.get(name) for name in _SAVED}
        try:
            os.environ.pop(_KEY_NAME, None)
            os.environ["GOOGLE_API_KEY"] = secret
            os.environ["AVI_GEMINI_MODEL"] = alias
            os.environ[_MODEL_NAME] = pin
            missing_key = gemini_integration_gate()
            self.assertEqual(missing_key, "GEMINI_API_KEY is unset; integration skipped")
            self.assertNotIn(secret, missing_key)
            self.assertNotIn(pin, missing_key)
            self.assertNotIn("GOOGLE_API_KEY", missing_key)
            self.assertNotIn("AVI_GEMINI_MODEL", missing_key)

            os.environ[_KEY_NAME] = secret
            os.environ.pop(_MODEL_NAME, None)
            with self.assertRaises(GeminiModelUnconfigured) as caught:
                gemini_integration_gate()
            self.assertIn("GEMINI_LIVE_MODEL", str(caught.exception))
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn(alias, str(caught.exception))

            os.environ[_MODEL_NAME] = pin
            self.assertIsNone(gemini_integration_gate())
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_provider_path_report_when_the_provider_exists(self):
        try:
            reason = gemini_integration_gate()
        except GeminiModelUnconfigured as exc:
            self.fail(str(exc))
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
        for name in (_KEY_NAME, _MODEL_NAME, *_IGNORED):
            value = os.environ.get(name)
            if value:
                self.assertNotIn(value, encoded)
