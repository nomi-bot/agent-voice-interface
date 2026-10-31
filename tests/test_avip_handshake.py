import copy
import json
from pathlib import Path
import unittest

from avi.protocols import avip


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "avip-v1-handshake.json").read_text()
)


def _string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.protocol = avip
        self.message = copy.deepcopy(FIXTURES["valid"][0]["message"])

    def test_shared_clients_negotiate_without_device_specific_rules(self):
        available = self.protocol.Capabilities(**FIXTURES["available_capabilities"])
        for case in FIXTURES["valid"]:
            with self.subTest(case=case["name"]):
                start = self.protocol.parse_session_start(case["message"])
                ready = self.protocol.session_ready(start, "session-001", available)
                self.assertEqual(ready, {
                    "protocol": "avip/1",
                    "type": "session.ready",
                    "request_id": case["message"]["request_id"],
                    "session_id": "session-001",
                    "capabilities": case["negotiated"],
                })

    def test_shared_invalid_messages_have_stable_errors(self):
        for case in FIXTURES["invalid"]:
            with self.subTest(case=case["name"]):
                with self.assertRaises(self.protocol.ProtocolError) as caught:
                    self.protocol.parse_session_start(case["message"])
                self.assertEqual(caught.exception.code, case["code"])
                self.assertEqual(caught.exception.field, case["field"])

    def test_required_envelope_fields_cannot_be_omitted(self):
        for field in self.message:
            with self.subTest(field=field):
                message = dict(self.message)
                del message[field]
                with self.assertRaises(self.protocol.ProtocolError):
                    self.protocol.parse_session_start(message)

    def test_unknown_envelope_fields_are_rejected(self):
        self.message["owner_id"] = "someone-else"
        with self.assertRaises(self.protocol.ProtocolError):
            self.protocol.parse_session_start(self.message)

    def test_client_identifiers_have_a_bounded_ascii_format(self):
        for field in ("request_id", "device_id"):
            for invalid in (None, True, 1, [], {}, "", " ", "a b", "\nsecret", "设备", "a" * 129):
                with self.subTest(field=field, value=invalid):
                    self.message[field] = invalid
                    with self.assertRaises(self.protocol.ProtocolError) as caught:
                        self.protocol.parse_session_start(self.message)
                    self.assertEqual(caught.exception.field, field)
                    self.message = copy.deepcopy(FIXTURES["valid"][0]["message"])

    def test_identifier_boundaries_are_accepted(self):
        self.message["request_id"] = "A"
        self.message["device_id"] = "a" * 128
        start = self.protocol.parse_session_start(self.message)
        self.assertEqual(start.request_id, "A")
        self.assertEqual(start.device_id, "a" * 128)

    def test_capability_values_are_strict_booleans_even_for_unknown_names(self):
        for capability in ("audio_input", "future_sensor"):
            for invalid in (1, 0, "true", None, [], {}):
                with self.subTest(capability=capability, value=invalid):
                    self.message["capabilities"] = {capability: invalid}
                    with self.assertRaises(self.protocol.ProtocolError) as caught:
                        self.protocol.parse_session_start(self.message)
                    self.assertEqual(caught.exception.field, "capabilities")

    def test_capabilities_must_be_an_object(self):
        for invalid in (None, [], "audio_input", True):
            with self.subTest(value=invalid):
                self.message["capabilities"] = invalid
                with self.assertRaises(self.protocol.ProtocolError):
                    self.protocol.parse_session_start(self.message)

    def test_optional_unknown_capabilities_are_ignored(self):
        self.message["capabilities"]["future_sensor"] = True
        start = self.protocol.parse_session_start(self.message)
        ready = self.protocol.session_ready(start, "session-001", self.protocol.Capabilities())
        self.assertNotIn("future_sensor", ready["capabilities"])
        self.assertFalse(any(ready["capabilities"].values()))

    def test_negotiation_requires_both_client_and_gateway_support(self):
        requested = self.protocol.Capabilities(audio_input=True)
        available = self.protocol.Capabilities(audio_output=True)
        self.assertEqual(
            self.protocol.negotiate_capabilities(requested, available),
            self.protocol.Capabilities(),
        )

    def test_parsed_message_is_independent_of_client_mutations(self):
        start = self.protocol.parse_session_start(self.message)
        self.message["capabilities"]["audio_input"] = False
        self.assertTrue(start.capabilities.audio_input)

    def test_ready_requires_a_valid_gateway_session_identifier(self):
        start = self.protocol.parse_session_start(self.message)
        for invalid in (None, "", "a" * 129, "a b"):
            with self.subTest(value=invalid):
                with self.assertRaises(self.protocol.ProtocolError) as caught:
                    self.protocol.session_ready(start, invalid, self.protocol.Capabilities())
                self.assertEqual(caught.exception.field, "session_id")

    def test_wire_envelopes_keep_only_allowlisted_codes_and_fields(self):
        for item in FIXTURES["wire_errors"]:
            envelope = item["envelope"]
            built = self.protocol.error_envelope(
                self.protocol.ProtocolError(envelope["code"], envelope["field"])
            )
            self.assertEqual(built, envelope)
        for case in FIXTURES["invalid"]:
            with self.subTest(case=case["name"]):
                with self.assertRaises(self.protocol.ProtocolError) as caught:
                    self.protocol.parse_session_start(case["message"])
                envelope = self.protocol.error_envelope(caught.exception)
                self.assertEqual(envelope, {
                    "protocol": "avip/1",
                    "type": "error",
                    "code": case["code"],
                    "field": case["field"],
                })
                encoded = json.dumps(envelope)
                for value in _string_values(case["message"]):
                    if value == "avip/1":
                        continue
                    self.assertNotIn(value, encoded)
        leaked = self.protocol.ProtocolError("invalid_message", "device_id")
        leaked.code = "LEAK9f3a"
        leaked.field = ["LEAK9f3a"]
        collapsed = self.protocol.error_envelope(leaked)
        self.assertEqual(collapsed, {
            "protocol": "avip/1",
            "type": "error",
            "code": "invalid_message",
            "field": None,
        })
        self.assertNotIn("LEAK9f3a", json.dumps(collapsed))

    def test_session_start_detection_is_only_the_type(self):
        for case in FIXTURES["valid"]:
            self.assertTrue(self.protocol.is_session_start(case["message"]))
        self.assertFalse(self.protocol.is_session_start([]))
        self.assertFalse(self.protocol.is_session_start(
            {"type": "agent.run.cancel", "protocol": "avip/1"}
        ))
        self.assertTrue(self.protocol.is_session_start(
            {"type": "session.start", "request_id": "not-validated"}
        ))

    def test_invalid_protocol_and_type_values_do_not_raise_type_errors(self):
        for field in ("protocol", "type"):
            for invalid in (None, [], {}, True):
                with self.subTest(field=field, value=invalid):
                    message = dict(self.message, **{field: invalid})
                    with self.assertRaises(self.protocol.ProtocolError):
                        self.protocol.parse_session_start(message)
