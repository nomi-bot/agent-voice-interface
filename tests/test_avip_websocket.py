import asyncio
import copy
import json
from pathlib import Path
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from avi.core.avip_session import allocate_session_id
from avi.main import AVIP_CAPABILITIES, app
from avi.protocols import avip
from avi.core.session import SessionLimits


FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "avip-v1-handshake.json").read_text()
)
TOKEN = "LEAK9f3a"
DISABLED = {
    "audio_input": False,
    "audio_output": False,
    "camera": False,
    "display": False,
    "robot_actions": False,
}


def wire_error(code, field):
    return {"protocol": "avip/1", "type": "error", "code": code, "field": field}


@asynccontextmanager
async def connection(path="/ws/avip", origin="http://localhost:8000", send_hook=None):
    """Drive a real ASGI WebSocket route without threads or network."""
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    headers = [] if origin is None else [(b"origin", origin.encode())]
    scope = {
        "type": "websocket", "asgi": {"version": "3.0"},
        "scheme": "ws", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8000),
        "subprotocols": [], "state": {},
    }

    async def output(event):
        if send_hook is not None:
            await send_hook(event)
        await outgoing.put(event)

    task = asyncio.create_task(app(scope, incoming.get, output))
    await incoming.put({"type": "websocket.connect"})
    try:
        yield incoming, outgoing
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        try:
            async with asyncio.timeout(1):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def reply(outgoing):
    async with asyncio.timeout(1):
        event = await outgoing.get()
    return json.loads(event["text"])


async def send(incoming, data):
    await incoming.put({"type": "websocket.receive", "text": json.dumps(data)})


async def send_frame(incoming, text=None, data=None):
    message = {"type": "websocket.receive"}
    if data is not None:
        message["bytes"] = data
    else:
        message["text"] = text
    await incoming.put(message)


class AvipWebSocketTests(unittest.IsolatedAsyncioTestCase):
    def test_session_ids_are_opaque_and_gateway_capabilities_stay_disabled(self):
        self.assertEqual(AVIP_CAPABILITIES, avip.Capabilities())
        allocated = {allocate_session_id() for _ in range(32)}
        self.assertEqual(len(allocated), 32)
        for session_id in allocated:
            self.assertRegex(session_id, r"^s[0-9a-f]{32}$")

    async def _accept(self, outgoing):
        event = await asyncio.wait_for(outgoing.get(), 1)
        self.assertEqual(event["type"], "websocket.accept")

    async def _close(self, outgoing, code, reason):
        event = await asyncio.wait_for(outgoing.get(), 1)
        self.assertEqual(event, {"type": "websocket.close", "code": code, "reason": reason})

    def _assert_token_absent(self, event):
        self.assertNotIn(TOKEN, json.dumps(event))

    async def test_development_gateway_negotiates_nothing_and_skips_the_agent(self):
        message = copy.deepcopy(FIXTURES["valid"][1]["message"])
        self.assertTrue(message["capabilities"]["audio_input"])
        self.assertTrue(message["capabilities"]["robot_actions"])
        self.assertTrue(message["capabilities"]["future_sensor"])
        with patch("avi.main.MockAgentAdapter", side_effect=AssertionError("no agent")):
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    await self._accept(outgoing)
                    await send(incoming, message)
                    ready = await reply(outgoing)
                    self.assertEqual(set(ready), {
                        "protocol", "type", "request_id", "session_id", "capabilities",
                    })
                    self.assertEqual(ready["protocol"], "avip/1")
                    self.assertEqual(ready["type"], "session.ready")
                    self.assertEqual(ready["request_id"], message["request_id"])
                    self.assertRegex(ready["session_id"], r"^s[0-9a-f]{32}$")
                    self.assertEqual(ready["capabilities"], DISABLED)
                    self.assertNotIn("future_sensor", ready["capabilities"])
                    self.assertEqual(app.state.active_sessions, 1)
                self.assertEqual(app.state.active_sessions, 0)

    async def test_shared_fixtures_negotiate_over_the_socket(self):
        available = avip.Capabilities(**FIXTURES["available_capabilities"])
        seen = set()
        with patch("avi.main.AVIP_CAPABILITIES", available):
            async with app.router.lifespan_context(app):
                for case in FIXTURES["valid"]:
                    with self.subTest(case=case["name"]):
                        async with connection() as (incoming, outgoing):
                            await self._accept(outgoing)
                            await send(incoming, case["message"])
                            ready = await reply(outgoing)
                            self.assertEqual(ready["request_id"], case["message"]["request_id"])
                            self.assertEqual(ready["capabilities"], case["negotiated"])
                            self.assertRegex(ready["session_id"], r"^s[0-9a-f]{32}$")
                            self.assertNotEqual(ready["session_id"], case["message"]["device_id"])
                            seen.add(ready["session_id"])
                self.assertEqual(len(seen), len(FIXTURES["valid"]))

    async def test_invalid_fixtures_have_stable_wire_errors(self):
        async with app.router.lifespan_context(app):
            for case in FIXTURES["invalid"]:
                with self.subTest(case=case["name"]):
                    async with connection() as (incoming, outgoing):
                        await self._accept(outgoing)
                        await send(incoming, case["message"])
                        error = await reply(outgoing)
                        self.assertEqual(error, wire_error(case["code"], case["field"]))
                        encoded = json.dumps(error)
                        for value in _string_values(case["message"]):
                            if value == "avip/1":
                                continue
                            self.assertNotIn(value, encoded)
                        await self._close(outgoing, 1002, "Protocol error")
            self.assertEqual(app.state.active_sessions, 0)

    async def test_rejected_values_are_absent_from_the_error_and_close(self):
        message = copy.deepcopy(FIXTURES["valid"][0]["message"])
        message["protocol"] = TOKEN
        message["request_id"] = TOKEN
        message["device_id"] = f"{TOKEN} {TOKEN}"
        async with app.router.lifespan_context(app):
            async with connection() as (incoming, outgoing):
                await self._accept(outgoing)
                await send(incoming, message)
                error = await reply(outgoing)
                self.assertEqual(error, wire_error("unsupported_protocol", "protocol"))
                self._assert_token_absent(error)
                close = await asyncio.wait_for(outgoing.get(), 1)
                self.assertEqual(close["code"], 1002)
                self._assert_token_absent(close)
            self.assertEqual(app.state.active_sessions, 0)

    async def test_non_json_and_binary_frames_are_discarded(self):
        frames = (
            ("text", TOKEN, None, 1002, "Protocol error"),
            ("bytes", None, b"\xff" + TOKEN.encode(), 1003, "Unsupported data"),
        )
        async with app.router.lifespan_context(app):
            for kind, text, data, code, reason in frames:
                with self.subTest(kind=kind):
                    async with connection() as (incoming, outgoing):
                        await self._accept(outgoing)
                        await send_frame(incoming, text=text, data=data)
                        error = await reply(outgoing)
                        self.assertEqual(error, wire_error("invalid_message", None))
                        self._assert_token_absent(error)
                        close = await asyncio.wait_for(outgoing.get(), 1)
                        self.assertEqual(
                            close, {"type": "websocket.close", "code": code, "reason": reason}
                        )
                        self._assert_token_absent(close)

    async def test_duplicate_start_does_not_replace_the_session(self):
        message = copy.deepcopy(FIXTURES["valid"][0]["message"])
        again = copy.deepcopy(message)
        again["request_id"] = TOKEN
        async with app.router.lifespan_context(app):
            async with connection() as (incoming, outgoing):
                await self._accept(outgoing)
                await send(incoming, message)
                ready = await reply(outgoing)
                await send(incoming, again)
                error = await reply(outgoing)
                self.assertEqual(error, wire_error("session_already_started", None))
                self._assert_token_absent(error)
                self.assertNotIn(ready["session_id"], json.dumps(error))
                await self._close(outgoing, 1002, "Protocol error")
            self.assertEqual(app.state.active_sessions, 0)

    async def test_disconnect_discards_the_session_id(self):
        message = copy.deepcopy(FIXTURES["valid"][2]["message"])
        async with app.router.lifespan_context(app):
            async with connection() as (incoming, outgoing):
                await self._accept(outgoing)
                await send(incoming, message)
                ready = await reply(outgoing)
                session_id = ready["session_id"]
            self.assertEqual(app.state.active_sessions, 0)
            resumed = copy.deepcopy(message)
            resumed["session_id"] = session_id
            resumed["request_id"] = "req-again"
            async with connection() as (incoming, outgoing):
                await self._accept(outgoing)
                await send(incoming, resumed)
                error = await reply(outgoing)
                self.assertEqual(error, wire_error("invalid_message", "session_id"))
                self.assertNotIn(session_id, json.dumps(error))
                await self._close(outgoing, 1002, "Protocol error")
            fresh = copy.deepcopy(message)
            fresh["request_id"] = "req-new"
            async with connection() as (incoming, outgoing):
                await self._accept(outgoing)
                await send(incoming, fresh)
                ready = await reply(outgoing)
                self.assertNotEqual(ready["session_id"], session_id)
                self.assertEqual(ready["type"], "session.ready")

    async def test_unsupported_text_after_ready_keeps_the_session(self):
        message = copy.deepcopy(FIXTURES["valid"][0]["message"])
        async with app.router.lifespan_context(app):
            async with connection() as (incoming, outgoing):
                await self._accept(outgoing)
                await send(incoming, message)
                ready = await reply(outgoing)
                await send(incoming, {"type": TOKEN, "request_id": TOKEN})
                error = await reply(outgoing)
                self.assertEqual(error, wire_error("unsupported_message_type", "type"))
                self._assert_token_absent(error)
                self.assertEqual(app.state.active_sessions, 1)
                with self.assertRaises(TimeoutError):
                    async with asyncio.timeout(0.05):
                        await outgoing.get()
                await send(incoming, {"protocol": "avip/1", "type": "playback.stop"})
                again = await reply(outgoing)
                self.assertEqual(again, wire_error("unsupported_message_type", "type"))
                self.assertRegex(ready["session_id"], r"^s[0-9a-f]{32}$")
            self.assertEqual(app.state.active_sessions, 0)

    async def test_idle_timeout_discards_a_connection_before_and_after_ready(self):
        message = copy.deepcopy(FIXTURES["valid"][0]["message"])
        limits = SessionLimits(idle_timeout=0.05)
        with patch("avi.main.SESSION_LIMITS", limits):
            async with app.router.lifespan_context(app):
                async with connection() as (_, outgoing):
                    await self._accept(outgoing)
                    await self._close(outgoing, 1001, "Idle timeout")
                self.assertEqual(app.state.active_sessions, 0)
                async with connection() as (incoming, outgoing):
                    await self._accept(outgoing)
                    await send(incoming, message)
                    self.assertEqual((await reply(outgoing))["type"], "session.ready")
                    await self._close(outgoing, 1001, "Idle timeout")
                self.assertEqual(app.state.active_sessions, 0)

    async def test_stalled_ready_send_closes_and_clears_the_count(self):
        entered = asyncio.Event()
        message = copy.deepcopy(FIXTURES["valid"][0]["message"])

        async def stall(event):
            if event["type"] == "websocket.send":
                entered.set()
                await asyncio.Event().wait()

        with patch("avi.main.SESSION_LIMITS", SessionLimits(send_timeout=0.05)):
            async with app.router.lifespan_context(app):
                async with connection(send_hook=stall) as (incoming, outgoing):
                    await self._accept(outgoing)
                    await send(incoming, message)
                    await asyncio.wait_for(entered.wait(), 1)
                    await self._close(outgoing, 1013, "Send timeout")
                self.assertEqual(app.state.active_sessions, 0)

    async def test_origin_is_required_on_the_avip_path(self):
        async with app.router.lifespan_context(app):
            for origin in (None, "https://untrusted.example"):
                with self.subTest(origin=origin):
                    async with connection(origin=origin) as (_, outgoing):
                        event = await asyncio.wait_for(outgoing.get(), 1)
                        self.assertEqual(
                            event, {"type": "websocket.close", "code": 1008, "reason": ""}
                        )
            self.assertEqual(app.state.active_sessions, 0)

    async def test_legacy_socket_stays_independent_of_avip(self):
        message = copy.deepcopy(FIXTURES["valid"][0]["message"])
        async with app.router.lifespan_context(app):
            async with connection("/ws/avip") as (avip_in, avip_out):
                await self._accept(avip_out)
                await send(avip_in, message)
                ready = await reply(avip_out)
                self.assertEqual(ready["type"], "session.ready")
                async with connection("/ws") as (legacy_in, legacy_out):
                    await self._accept(legacy_out)
                    legacy = await reply(legacy_out)
                    self.assertEqual(legacy, {"type": "ready", "agent": "mock", "voice": False})
                    await send(legacy_in, message)
                    rejected = await reply(legacy_out)
                    self.assertEqual(rejected, {
                        "type": "error",
                        "message": "Invalid message or unknown run",
                    })
                    self.assertEqual(app.state.active_sessions, 2)
                await send(avip_in, {"type": "ping"})
                self.assertEqual(
                    await reply(avip_out), wire_error("unsupported_message_type", "type")
                )
                self.assertEqual(app.state.active_sessions, 1)
            self.assertEqual(app.state.active_sessions, 0)


def _string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
