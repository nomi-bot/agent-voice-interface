import asyncio
import json
import unittest
from contextlib import asynccontextmanager

import httpx

from avi.main import app


@asynccontextmanager
async def connection(origin="http://localhost:8000"):
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
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
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
