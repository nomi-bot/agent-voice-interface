"""JS and Python AVIP encoders, compared without sharing one serializer.

``avip_client`` writes frames with ``int.to_bytes``. ``clients/browser/avip-client.js``
writes them with ``DataView``. The gateway still uses ``struct``. The in-process
browser double uses its own ``struct`` call. This module runs the browser file
under Node and requires the session.start text and binary frames to match.

There is no intentional wire difference. A JavaScript timestamp above
``Number.MAX_SAFE_INTEGER`` is passed as a bigint; the uint64 on the wire is the
same integer the Python encoder writes.
"""
import ast
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
from unittest.mock import patch

from avi.main import app
from avi.protocols import avip
from avi.protocols.media import Direction, MediaFrame, pack_frame as gateway_pack
from avip_client import (
    AUDIO_DOWNLINK,
    AUDIO_UPLINK,
    AvipClient,
    AvipError,
    encode_session_start,
    pack_frame,
)
import avip_client
from avip_clients import HANDSHAKE, MEDIA, BrowserShapedClient
from test_avip_websocket import connection, send_frame


ROOT = Path(__file__).parents[1]
SDK_ROOT = Path(avip_client.__file__).resolve().parent
BROWSER = ROOT / "clients" / "browser" / "avip-client.js"
CLI = ROOT / "clients" / "browser" / "cli.mjs"
_DIRECTIONS = {"audio_uplink": AUDIO_UPLINK, "audio_downlink": AUDIO_DOWNLINK}


def _node(args, stdin=None, timeout=15):
    completed = subprocess.run(
        ["node", *args],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout


def _encode(frames, handshakes, rejected=None):
    payload = {"frames": frames, "handshakes": handshakes, "rejected": rejected or []}
    return json.loads(_node([str(CLI), "encode"], json.dumps(payload)))


def _case(direction, sequence, timestamp_us, payload: bytes):
    if isinstance(direction, str):
        direction = _DIRECTIONS[direction]
    return {
        "direction": direction,
        "sequence": sequence,
        "timestamp_us": str(timestamp_us),
        "payload_hex": payload.hex(),
    }


class SdkConformanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._voice_env = patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "",
                "GEMINI_LIVE_MODEL": "",
                "AVIP_MOCK_MEDIA": "",
            },
            clear=False,
        )
        self._voice_env.start()
        self.addCleanup(self._voice_env.stop)

    def test_sdk_does_not_import_the_gateway_serializer(self):
        for path in sorted(SDK_ROOT.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                for module in modules:
                    self.assertFalse(
                        module == "avi" or module.startswith("avi."),
                        f"{path.name} imports {module}",
                    )
            self.assertNotIn("import struct", path.read_text())
        adapter = BROWSER.read_text()
        self.assertNotIn("avi.protocols", adapter)
        self.assertNotIn("pack_frame", adapter)
        self.assertNotIn("avip_client", adapter)
        self.assertIsNot(pack_frame, gateway_pack)

    def test_js_and_python_frames_and_handshakes_match(self):
        two = b"\x00\x00"
        frames = [
            _case(vector["direction"], vector["sequence"], vector["timestamp_us"], bytes.fromhex(vector["payload_hex"]))
            for vector in MEDIA["vectors"]
        ]
        frames.extend([
            _case(AUDIO_UPLINK, 0, 0, bytes(MEDIA["nominal_payload_bytes"])),
            _case(AUDIO_UPLINK, 0xFFFFFFFF, 0, two),
            _case(AUDIO_DOWNLINK, 1, 20_000, b"\xff\x7f"),
            _case(AUDIO_UPLINK, 2, 2**53, two),
            _case(AUDIO_DOWNLINK, 3, 2**64 - 1, two),
            _case(AUDIO_UPLINK, 4, 72_623_859_790_382_856, bytes(640)),
            _case(AUDIO_UPLINK, 5, 0, bytes(8192)),
        ])
        handshakes = [dict(item["message"]) for item in HANDSHAKE["valid"]]
        handshakes.append({
            "device_id": "browser-demo-001",
            "session_id": None,
            "capabilities": {
                "display": False,
                "🤖": True,
                "future_sensor": False,
                "audio_input": True,
            },
            "request_id": "req-order-001",
            "type": "session.start",
            "protocol": "avip/1",
        })
        rejected_message = dict(HANDSHAKE["valid"][0]["message"])
        rejected_message["capabilities"] = {"audio_input": 1}
        encoded = _encode(frames, handshakes, [rejected_message])
        browser = BrowserShapedClient()
        self.assertEqual(len(encoded["frames"]), len(frames))
        for case, result in zip(frames, encoded["frames"], strict=True):
            payload = bytes.fromhex(case["payload_hex"])
            python_frame = pack_frame(
                case["direction"], case["sequence"], int(case["timestamp_us"]), payload
            )
            browser_frame = browser.pack(
                case["direction"], case["sequence"], int(case["timestamp_us"]), payload
            )
            gateway_frame = gateway_pack(MediaFrame(
                Direction(case["direction"]),
                case["sequence"],
                int(case["timestamp_us"]),
                payload,
            ))
            self.assertEqual(python_frame.hex(), result["frame_hex"])
            self.assertEqual(browser_frame, python_frame)
            self.assertEqual(gateway_frame, python_frame)
            self.assertEqual(result["direction"], case["direction"])
            self.assertEqual(result["sequence"], case["sequence"])
            self.assertEqual(result["timestamp_us"], case["timestamp_us"])
            self.assertEqual(result["payload_hex"], case["payload_hex"])
        for vector in MEDIA["vectors"]:
            self.assertIn(vector["frame_hex"], {item["frame_hex"] for item in encoded["frames"]})
        self.assertEqual(len(encoded["handshakes"]), len(handshakes))
        for message, text in zip(handshakes, encoded["handshakes"], strict=True):
            python_text = encode_session_start(message)
            self.assertEqual(text, python_text)
            self.assertNotIn(" ", python_text)
            parsed = avip.parse_session_start(json.loads(python_text))
            self.assertEqual(parsed.request_id, message["request_id"])
            self.assertEqual(parsed.device_id, message["device_id"])
        ordered = encoded["handshakes"][-1]
        self.assertLess(ordered.index('"audio_input"'), ordered.index('"display"'))
        self.assertLess(ordered.index('"display"'), ordered.index('"future_sensor"'))
        self.assertLess(ordered.index('"future_sensor"'), ordered.index('"🤖"'))
        with self.assertRaises(AvipError) as caught:
            encode_session_start(rejected_message)
        self.assertEqual(encoded["rejected"], [str(caught.exception)])
        self.assertEqual(caught.exception.code, "invalid_message")
        self.assertEqual(caught.exception.field, "capabilities")

    async def test_gateway_accepts_sdk_bytes_on_the_shared_route(self):
        message = HANDSHAKE["valid"][1]["message"]
        text = encode_session_start(message)
        payload = bytes.fromhex(MEDIA["vectors"][0]["payload_hex"])
        uplink = pack_frame(AUDIO_UPLINK, 0, 0, payload)
        expected = pack_frame(AUDIO_DOWNLINK, 0, 0, payload)
        with patch("avi.main.AVIP_CAPABILITIES", avip.Capabilities(**HANDSHAKE["available_capabilities"])):
            async with app.router.lifespan_context(app):
                async with connection() as (incoming, outgoing):
                    event = await asyncio.wait_for(outgoing.get(), 1)
                    self.assertEqual(event["type"], "websocket.accept")
                    await send_frame(incoming, text=text)
                    ready = await self._json(outgoing)
                    self.assertEqual(ready["type"], "session.ready")
                    self.assertEqual(ready["request_id"], message["request_id"])
                    self.assertEqual(ready["capabilities"], HANDSHAKE["valid"][1]["negotiated"])
                    await send_frame(incoming, data=uplink)
                    self.assertEqual(await self._bytes(outgoing), expected)

    async def test_live_python_and_browser_clients_match_on_one_gateway(self):
        payload = bytes.fromhex(MEDIA["vectors"][0]["payload_hex"])
        expected = pack_frame(AUDIO_DOWNLINK, 0, 0, payload)
        port = _free_port()
        log = tempfile.TemporaryFile(mode="w+")
        process = subprocess.Popen(
            [
                _python(), "-m", "uvicorn", "avi.main:app",
                "--host", "127.0.0.1", "--port", str(port),
                "--ws-max-size", "16384", "--ws-max-queue", "16",
            ],
            env=_mock_media_env(),
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=ROOT,
        )
        try:
            await _wait_health(port, process, log)
            python_downlink = await self._python_live(port, payload)
            browser_stdout = await asyncio.to_thread(
                _node,
                [str(CLI), "roundtrip", f"ws://127.0.0.1:{port}/ws/avip", payload.hex()],
                None,
                10,
            )
            browser_report = json.loads(browser_stdout)
        except Exception as exc:
            process.kill()
            log.seek(0)
            raise AssertionError(f"{type(exc).__name__}: {exc}\n{log.read()}") from exc
        finally:
            await asyncio.to_thread(_stop_process, process)
            log.close()
        self.assertEqual(python_downlink, expected)
        self.assertEqual(bytes.fromhex(browser_report["downlink_hex"]), expected)
        self.assertEqual(
            bytes.fromhex(browser_report["uplink_hex"]),
            pack_frame(AUDIO_UPLINK, 0, 0, payload),
        )
        self.assertEqual(browser_report["request_id"], "req-browser-001")
        self.assertEqual(browser_report["capabilities"], HANDSHAKE["valid"][0]["negotiated"])
        self.assertEqual(browser_report["direction"], AUDIO_DOWNLINK)
        self.assertEqual(browser_report["sequence"], 0)
        self.assertEqual(browser_report["timestamp_us"], "0")
        self.assertEqual(browser_report["payload_hex"], payload.hex())
        self.assertRegex(browser_report["session_id"], r"^s[0-9a-f]{32}$")

    async def _python_live(self, port, payload):
        async with AvipClient(
            f"ws://127.0.0.1:{port}/ws/avip",
            request_id="req-python-001",
            device_id="headless-sim-001",
            capabilities={
                "audio_input": True,
                "audio_output": True,
                "robot_actions": True,
                "future_sensor": True,
            },
        ) as client:
            ready = await asyncio.wait_for(client.start(), 5)
            self.assertEqual(ready["capabilities"], HANDSHAKE["valid"][1]["negotiated"])
            self.assertRegex(ready["session_id"], r"^s[0-9a-f]{32}$")
            await client.send_uplink(payload)
            frame = await asyncio.wait_for(client.recv(), 5)
        self.assertEqual(frame.direction, AUDIO_DOWNLINK)
        self.assertEqual(frame.sequence, 0)
        self.assertEqual(frame.timestamp_us, 0)
        self.assertEqual(frame.payload, payload)
        return pack_frame(AUDIO_DOWNLINK, frame.sequence, frame.timestamp_us, frame.payload)

    async def _json(self, outgoing):
        event = await asyncio.wait_for(outgoing.get(), 1)
        self.assertIn("text", event)
        return json.loads(event["text"])

    async def _bytes(self, outgoing):
        event = await asyncio.wait_for(outgoing.get(), 1)
        self.assertIn("bytes", event)
        return event["bytes"]


def _python():
    import sys
    return sys.executable


def _mock_media_env():
    import os
    env = os.environ.copy()
    env["AVIP_MOCK_MEDIA"] = "1"
    for name in ("GEMINI_API_KEY", "GEMINI_LIVE_MODEL", "GOOGLE_API_KEY"):
        env.pop(name, None)
    return env


def _stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_health(port, process, log):
    deadline = time.monotonic() + 10
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.seek(0)
            raise AssertionError(log.read())
        try:
            await asyncio.to_thread(_health_ok, url)
            return
        except OSError:
            await asyncio.sleep(0.05)
    log.seek(0)
    raise AssertionError(log.read() or "gateway did not become healthy")


def _health_ok(url):
    with urllib.request.urlopen(url, timeout=0.5) as response:
        if response.status != 200:
            raise OSError(response.status)
