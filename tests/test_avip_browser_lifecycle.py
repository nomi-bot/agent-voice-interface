"""Browser client lifecycle and uplink-clock acceptance on both SDKs."""
import asyncio
import json
from pathlib import Path
import socket
import unittest

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from avip_client import AUDIO_DOWNLINK, AUDIO_UPLINK, AvipClient, AvipError, pack_frame


ROOT = Path(__file__).parents[1]
LIFECYCLE = ROOT / "clients" / "browser" / "lifecycle.mjs"
READY = json.dumps({
    "protocol": "avip/1",
    "type": "session.ready",
    "request_id": "req-browser-001",
    "session_id": "s" + "ab" * 16,
    "capabilities": {
        "audio_input": True,
        "audio_output": True,
        "camera": False,
        "display": False,
        "robot_actions": False,
    },
}, separators=(",", ":"))


class _ScriptedSocket:
    def __init__(self, client):
        self.client = client
        self.sent = []
        self.sequence_during_send = None
        self.fail = False

    async def send(self, data):
        self.sequence_during_send = self.client._clock.sequence
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(data)

    async def close(self):
        return None


class _ClosingSocket:
    async def send(self, data):
        raise ConnectionClosed(Close(1000, ""), None)

    async def recv(self):
        raise ConnectionClosed(Close(1000, ""), None)

    async def close(self):
        return None


class ClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_python_clock_advances_only_after_send_accepts_the_frame(self):
        client = _client()
        socket = _ScriptedSocket(client)
        socket.fail = True
        client._ws = socket
        client._started = True
        with self.assertRaises(RuntimeError):
            await client.send_uplink(b"\x00\x00")
        self.assertEqual(client._clock.sequence, 0)
        self.assertEqual(client._clock.timestamp_us, 0)
        self.assertEqual(socket.sent, [])
        socket.fail = False
        await client.send_uplink(b"\x00\x00")
        self.assertEqual(socket.sequence_during_send, 0)
        self.assertEqual(client._clock.sequence, 1)
        self.assertEqual(client._clock.timestamp_us, 20_000)
        self.assertEqual(socket.sent, [pack_frame(AUDIO_UPLINK, 0, 0, b"\x00\x00")])

    async def test_python_capture_gap_advances_time_without_a_sequence(self):
        client = _client()
        socket = _ScriptedSocket(client)
        client._ws = socket
        client._started = True
        client.note_capture_gap(40_000)
        self.assertEqual(client._clock.sequence, 0)
        self.assertEqual(client._clock.timestamp_us, 40_000)
        await client.send_uplink(b"\x00\x00")
        self.assertEqual(
            socket.sent, [pack_frame(AUDIO_UPLINK, 0, 40_000, b"\x00\x00")]
        )
        self.assertEqual(client._clock.sequence, 1)
        self.assertEqual(client._clock.timestamp_us, 60_000)
        client.note_capture_gap(0)
        self.assertEqual(client._clock.timestamp_us, 60_000)
        with self.assertRaises(AvipError):
            client.note_capture_gap(-1)

    async def test_python_closed_socket_does_not_retry_the_sequence(self):
        client = _client()
        client._ws = _ClosingSocket()
        client._started = True
        with self.assertRaises(ConnectionClosed):
            await client.send_uplink(b"\x00\x00")
        self.assertTrue(client.closed)
        self.assertEqual(client._clock.sequence, 0)
        with self.assertRaises(AvipError) as caught:
            await client.send_uplink(b"\x01\x02")
        self.assertEqual(caught.exception.code, "socket_closed")
        self.assertEqual(client._clock.sequence, 0)

    async def test_browser_clock_and_close_reject_every_waiter(self):
        report = json.loads(await _node("clock"))
        self.assertEqual(report["sendError"], "send failed")
        self.assertEqual(report["afterFail"], {"sequence": 0, "timestamp": "0"})
        self.assertEqual(report["afterOk"], {"sequence": 1, "timestamp": "20000"})
        self.assertEqual(report["state"], "closed")
        self.assertEqual(report["settled"], ["rejected", "rejected"])
        self.assertEqual(report["reasons"], ["socket closed", "socket closed"])
        self.assertEqual(report["third"], "socket closed")


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_during_handshake_rejects_start(self):
        async with _bound(handshake_then_close) as port:
            report = json.loads(await _node("handshake", f"ws://127.0.0.1:{port}/"))
        self.assertEqual(report["state"], "closed")
        self.assertFalse(report["started"])
        self.assertIn(report["message"], {"socket closed", "socket error"})

    async def test_disconnect_during_next_frame_rejects_every_waiter(self):
        async with _bound(ready_then_close) as port:
            report = json.loads(await _node("next-frame", f"ws://127.0.0.1:{port}/"))
        self.assertEqual(report["state"], "closed")
        self.assertEqual(report["settled"], ["rejected", "rejected"])
        self.assertEqual(len(report["reasons"]), 2)
        self.assertTrue(set(report["reasons"]) <= {"socket closed", "socket error"})
        self.assertEqual(report["third"], report["reasons"][0])

    async def test_slow_consumer_stops_at_the_receive_bound(self):
        async with _bound(flood) as port:
            report = json.loads(await _node("slow", f"ws://127.0.0.1:{port}/"))
        self.assertEqual(report["bound"], 8)
        self.assertEqual(report["state"], "closed")
        self.assertEqual(report["failure"], "queue_full")
        self.assertEqual(report["pending"], 0)
        self.assertEqual(report["drained"], list(range(8)))
        self.assertEqual(report["after"], "queue_full")


def _client():
    return AvipClient(
        "ws://127.0.0.1/ws/avip",
        request_id="req-python-001",
        device_id="headless-sim-001",
        capabilities={"audio_input": True, "audio_output": True},
    )


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _bound:
    def __init__(self, handler):
        self.handler = handler
        self.server = None

    async def __aenter__(self):
        port = _free_port()
        self.server = await serve(self.handler, "127.0.0.1", port)
        return port

    async def __aexit__(self, exc_type, exc, tb):
        self.server.close()
        await self.server.wait_closed()
        return False


async def handshake_then_close(ws):
    await asyncio.wait_for(ws.recv(), 2)
    await ws.close()


async def ready_then_close(ws):
    await asyncio.wait_for(ws.recv(), 2)
    await ws.send(READY)
    arm = await asyncio.wait_for(ws.recv(), 2)
    if arm != "arm":
        raise RuntimeError(repr(arm))
    await ws.close()


async def flood(ws):
    await asyncio.wait_for(ws.recv(), 2)
    await ws.send(READY)
    payload = b"\x00\x00"
    for sequence in range(9):
        await ws.send(pack_frame(AUDIO_DOWNLINK, sequence, sequence * 20_000, payload))
    await ws.wait_closed()


async def _node(*args):
    process = await asyncio.create_subprocess_exec(
        "node", str(LIFECYCLE), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
    if process.returncode != 0:
        raise AssertionError(stderr.decode() or stdout.decode())
    return stdout.decode()
