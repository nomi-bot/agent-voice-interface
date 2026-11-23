import asyncio
import json
from pathlib import Path
import subprocess
import unittest

from avi.main import app
from avip_client import encode_session_start
from test_avip_websocket import connection, reply, send, send_frame


_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "jev28-durable-runs-v1.json").read_text()
)
_BROWSER_CLI = Path(__file__).parents[1] / "clients" / "browser" / "cli.mjs"


class DurableWireCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_browser_and_headless_offer_the_same_opt_in_and_legacy_handshakes(self):
        starts = [_FIXTURE["positive"]["start"], _FIXTURE["legacy"]["start"]]
        browser = subprocess.run(
            ["node", str(_BROWSER_CLI), "encode"],
            input=json.dumps({"handshakes": starts, "frames": []}),
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )
        self.assertEqual(
            json.loads(browser.stdout)["handshakes"],
            [encode_session_start(message) for message in starts],
        )

    async def test_current_gateway_refuses_extension_and_keeps_the_session_usable(self):
        async with app.router.lifespan_context(app):
            async with connection() as (incoming, outgoing):
                accepted = await asyncio.wait_for(outgoing.get(), 1)
                self.assertEqual(accepted["type"], "websocket.accept")
                await send(incoming, _FIXTURE["positive"]["start"])
                self.assertEqual((await reply(outgoing))["capabilities"], _FIXTURE["legacy"]["capabilities"])
                await send(incoming, _FIXTURE["unsupported"]["request"])
                self.assertEqual(await reply(outgoing), _FIXTURE["unsupported"]["reply"])
                self.assertEqual(app.state.active_sessions, 1)
                await send_frame(incoming, text=_FIXTURE["malformed"]["text"])
                self.assertEqual(await reply(outgoing), _FIXTURE["malformed"]["reply"])
                closed = await asyncio.wait_for(outgoing.get(), 1)
                self.assertEqual(closed, {"type": "websocket.close", **_FIXTURE["malformed"]["close"]})
