import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch

import httpx

from avi.main import app
from avi.agents.mock import MockAgentAdapter
from avi.core.session import SessionLimits


@asynccontextmanager
async def connection(origin="http://localhost:8000", send_hook=None):
    """Drive the real ASGI WebSocket endpoint without threads or network."""
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    headers = [] if origin is None else [(b"origin", origin.encode())]
    scope = {
        "type": "websocket", "asgi": {"version": "3.0"},
        "scheme": "ws", "path": "/ws", "raw_path": b"/ws",
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


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_reply_queue_closes_and_cancels_writer(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def stall(event):
            if event["type"] == "websocket.send" and json.loads(event["text"])["type"] == "pong":
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        with patch("avi.main.SESSION_LIMITS", SessionLimits(pending_replies=1)):
            async with app.router.lifespan_context(app):
                async with connection(send_hook=stall) as (incoming, outgoing):
                    await outgoing.get()
                    await reply(outgoing)
                    await send(incoming, {"type": "ping"})
                    await asyncio.wait_for(entered.wait(), 1)
                    await send(incoming, {"type": "ping"})
                    await send(incoming, {"type": "ping"})
                    event = await asyncio.wait_for(outgoing.get(), 1)
                    self.assertEqual(event["code"], 1013)
                    self.assertEqual(event["reason"], "Reply queue full")
                    self.assertTrue(cancelled.is_set())
                self.assertEqual(app.state.active_sessions, 0)

    async def test_stalled_close_frame_is_bounded(self):
        close_cancelled = asyncio.Event()

        async def stall(event):
            if event["type"] == "websocket.close":
                try:
                    await asyncio.Event().wait()
                finally:
                    close_cancelled.set()

        limits = SessionLimits(idle_timeout=0.05, send_timeout=0.05)
        with patch("avi.main.SESSION_LIMITS", limits):
            async with app.router.lifespan_context(app):
                async with connection(send_hook=stall) as (_, outgoing):
                    await outgoing.get()
                    await reply(outgoing)
                    await asyncio.wait_for(close_cancelled.wait(), 1)
                self.assertEqual(app.state.active_sessions, 0)

    async def test_cleanup_timeout_does_not_leak_session_count(self):
        cancelled = asyncio.Event()

        class SlowCloseAgent(MockAgentAdapter):
            async def close(self):
                await super().close()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        limits = SessionLimits(cleanup_timeout=0.05)
        with patch("avi.main.MockAgentAdapter", SlowCloseAgent), patch("avi.main.SESSION_LIMITS", limits):
            async with app.router.lifespan_context(app):
                with self.assertLogs("avi.core.session", level="ERROR"):
                    async with connection() as (_, outgoing):
                        await outgoing.get()
                        await reply(outgoing)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(app.state.active_sessions, 0)

    async def test_operation_timeout_preserves_session(self):
        cancelled = asyncio.Event()

        class SlowAgent(MockAgentAdapter):
            async def status(self, run_id):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        limits = SessionLimits(operation_timeout=0.05)
        with patch("avi.main.MockAgentAdapter", SlowAgent), patch("avi.main.SESSION_LIMITS", limits):
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    await outgoing.get()
                    await reply(outgoing)
                    await send(incoming, {"type": "status", "run_id": "slow"})
                    self.assertEqual((await reply(outgoing))["message"], "Agent operation timed out")
                    self.assertTrue(cancelled.is_set())
                    await send(incoming, {"type": "submit", "request": "after timeout"})
                    self.assertEqual((await reply(outgoing))["type"], "run")

    async def test_full_operation_queue_still_accepts_stop(self):
        entered = asyncio.Event()

        class SlowAgent(MockAgentAdapter):
            async def status(self, run_id):
                entered.set()
                await asyncio.Event().wait()

        with patch("avi.main.MockAgentAdapter", SlowAgent), patch(
            "avi.main.SESSION_LIMITS", SessionLimits(pending_operations=1)
        ):
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    await outgoing.get()
                    await reply(outgoing)
                    await send(incoming, {"type": "status", "run_id": "slow"})
                    await asyncio.wait_for(entered.wait(), 1)
                    await send(incoming, {"type": "submit", "request": "queued"})
                    await send(incoming, {"type": "submit", "request": "over limit"})
                    self.assertEqual((await reply(outgoing))["message"], "Too many pending operations")
                    await send(incoming, {"type": "stop_playback"})
                    self.assertEqual((await reply(outgoing))["type"], "stop_playback")

    async def test_idle_timeout_cancels_active_operation(self):
        cancelled = asyncio.Event()

        class SlowAgent(MockAgentAdapter):
            async def status(self, run_id):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        with patch("avi.main.MockAgentAdapter", SlowAgent), patch(
            "avi.main.SESSION_LIMITS", SessionLimits(idle_timeout=0.05)
        ):
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    await outgoing.get()
                    await reply(outgoing)
                    await send(incoming, {"type": "status", "run_id": "slow"})
                    event = await asyncio.wait_for(outgoing.get(), 1)
                    self.assertEqual(event["code"], 1001)
                    self.assertTrue(cancelled.is_set())
                self.assertEqual(app.state.active_sessions, 0)

    async def test_stalled_reply_has_deadline(self):
        async def stall(event):
            if event["type"] == "websocket.send" and json.loads(event["text"])["type"] == "pong":
                await asyncio.Event().wait()

        with patch("avi.main.SESSION_LIMITS", SessionLimits(send_timeout=0.05)):
            async with app.router.lifespan_context(app):
                async with connection(send_hook=stall) as (incoming, outgoing):
                    await outgoing.get()
                    await reply(outgoing)
                    await send(incoming, {"type": "ping"})
                    event = await asyncio.wait_for(outgoing.get(), 1)
                    self.assertEqual(event["code"], 1013)
                    self.assertEqual(event["reason"], "Send timeout")
                self.assertEqual(app.state.active_sessions, 0)

    async def test_transport_write_failure_cleans_session(self):
        failed = asyncio.Event()

        async def fail(event):
            if event["type"] == "websocket.send" and json.loads(event["text"])["type"] == "pong":
                failed.set()
                raise OSError("disconnected")

        async with app.router.lifespan_context(app):
            async with connection(send_hook=fail) as (incoming, outgoing):
                await outgoing.get()
                await reply(outgoing)
                await send(incoming, {"type": "ping"})
                await asyncio.wait_for(failed.wait(), 1)
            self.assertEqual(app.state.active_sessions, 0)

    async def test_session_limits_reject_unbounded_or_invalid_settings(self):
        for name in ("pending_operations", "pending_replies", "send_timeout",
                     "idle_timeout", "operation_timeout", "cleanup_timeout"):
            values = (0, -1, 0.5) if name.startswith("pending_") else (0, -1, float("inf"), float("nan"))
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        replace(SessionLimits(), **{name: value})

    async def test_invalid_discriminator_preserves_session_and_run(self):
        with patch("avi.main.MockAgentAdapter", lambda: MockAgentAdapter(delay=60)):
            await self._check_invalid_discriminators()

    async def _check_invalid_discriminators(self):
        async with app.router.lifespan_context(app):
            async with connection() as (incoming, outgoing):
                await outgoing.get()
                await reply(outgoing)
                await send(incoming, {"type": "submit", "request": "keep running"})
                run = await reply(outgoing)
                for kind in ([], {}):
                    await send(incoming, {"type": kind})
                    self.assertEqual((await reply(outgoing))["type"], "error")
                    await send(incoming, {"type": "ping"})
                    self.assertEqual((await reply(outgoing))["type"], "pong")
                    await send(incoming, {"type": "status", "run_id": run["id"]})
                    self.assertEqual((await reply(outgoing))["status"], "running")

    async def test_slow_status_does_not_block_controls_or_disconnect(self):
        entered, cancelled, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        agents = []

        class SlowAgent(MockAgentAdapter):
            def __init__(self):
                super().__init__(delay=60)
                agents.append(self)

            async def status(self, run_id):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            async def close(self):
                await super().close()
                closed.set()

        with patch("avi.main.MockAgentAdapter", SlowAgent):
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    await outgoing.get()
                    await reply(outgoing)
                    await send(incoming, {"type": "submit", "request": "pending"})
                    run = await reply(outgoing)
                    await send(incoming, {"type": "status", "run_id": run["id"]})
                    await asyncio.wait_for(entered.wait(), 1)
                    await send(incoming, {"type": "stop_playback"})
                    self.assertEqual((await reply(outgoing))["type"], "stop_playback")
                    self.assertEqual(agents[0]._runs[run["id"]].status, "running")
                    await send(incoming, {"type": "ping"})
                    self.assertEqual((await reply(outgoing))["type"], "pong")
                self.assertTrue(cancelled.is_set())
                self.assertTrue(closed.is_set())
                self.assertTrue(all(task.done() for task in agents[0]._tasks.values()))
                self.assertEqual(app.state.active_sessions, 0)

    async def test_stalled_ready_send_has_deadline_and_cleans_session(self):
        entered = asyncio.Event()

        async def stall(event):
            if event["type"] == "websocket.send":
                entered.set()
                await asyncio.Event().wait()

        # Use a short deadline without waiting for the production timeout.
        with patch("avi.main.SESSION_LIMITS", SessionLimits(send_timeout=0.05)):
            async with app.router.lifespan_context(app):
                async with connection(send_hook=stall) as (_, outgoing):
                    self.assertEqual((await outgoing.get())["type"], "websocket.accept")
                    await asyncio.wait_for(entered.wait(), 1)
                    event = await asyncio.wait_for(outgoing.get(), 0.5)
                    self.assertEqual(event["type"], "websocket.close")
                self.assertEqual(app.state.active_sessions, 0)

    async def test_health(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            self.assertEqual((await client.get("/health")).json()["voice"], "not_implemented")

    async def test_isolation_errors_and_cleanup(self):
        async with app.router.lifespan_context(app):
            async with connection() as (one_in, one_out):
                self.assertEqual((await one_out.get())["type"], "websocket.accept")
                self.assertFalse((await reply(one_out))["voice"])
                await send(one_in, {"type": "submit", "request": "demo"})
                run = await reply(one_out)
                async with connection() as (two_in, two_out):
                    await two_out.get()
                    await reply(two_out)
                    await send(two_in, {"type": "status", "run_id": run["id"]})
                    self.assertEqual((await reply(two_out))["type"], "error")
                    await send(two_in, [])
                    self.assertEqual((await reply(two_out))["type"], "error")
                    await send(two_in, {"type": "ping"})
                    self.assertEqual((await reply(two_out))["type"], "pong")
                self.assertEqual(app.state.active_sessions, 1)
            self.assertEqual(app.state.active_sessions, 0)

    async def test_reject_foreign_or_missing_origin(self):
        for origin in (None, "https://untrusted.example"):
            async with connection(origin) as (_, outgoing):
                event = await asyncio.wait_for(outgoing.get(), timeout=1)
                self.assertEqual(event, {"type": "websocket.close", "code": 1008, "reason": ""})
