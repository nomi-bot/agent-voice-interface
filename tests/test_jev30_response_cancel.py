import asyncio
import unittest

from avi.core.avip_session import AvipSession
from avi.core.session import SessionLimits
from avi.protocols.avip import Capabilities
from test_avip_live_mount import Gate, _start
from test_gemini_live_provider import FakeSession, _audio_message, _Content, _Message, _pcm, _provider


class ResponseCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_targets_only_the_current_answer_on_the_negotiated_session(self):
        upstream = FakeSession()
        provider = _provider(upstream)
        gate = Gate()
        features = Capabilities(audio_input=True, audio_output=True)
        task = asyncio.create_task(AvipSession(gate, SessionLimits(), features, provider=provider).run())
        start = _start()
        start["capabilities"].update(audio_output=True, response_cancel=True)
        cancel = {"protocol": "avip/1", "type": "response.cancel", "response_id": 0}
        try:
            async with asyncio.timeout(3):
                await gate.inbox.put(("text", start))
                self.assertTrue((await gate.next_item())["capabilities"]["response_cancel"])
                await gate.inbox.put(("text", cancel))
                await gate.inbox.put(("text", {**cancel, "response_id": True}))
                self.assertEqual((await gate.next_item())["code"], "invalid_message")
                self.assertEqual(upstream.turns, [])
                await upstream.push(_audio_message(_pcm(7, 480)))
                self.assertEqual(await gate.next_item(), {"protocol": "avip/1", "type": "response.started", "response_id": 0})
                self.assertIsInstance(await gate.next_item(), bytes)
                await gate.inbox.put(("text", cancel))
                self.assertEqual((await gate.next_item())["type"], "response.ended")
                self.assertEqual((await gate.next_item())["type"], "response.interrupted")
                await upstream.push(_Message(server_content=_Content([], turn_complete=True)))
                await upstream.push(_audio_message(_pcm(9, 480)))
                self.assertEqual((await gate.next_item())["response_id"], 1)
                self.assertIsInstance(await gate.next_item(), bytes)
                await gate.inbox.put(("text", cancel))
                await gate.inbox.put(("text", {**cancel, "protocol": "wrong"}))
                self.assertEqual((await gate.next_item())["code"], "unsupported_protocol")
                self.assertEqual(upstream.turns, [(None, True)])
                self.assertFalse(upstream.closed)
        finally:
            await gate.inbox.put(None)
            await asyncio.wait_for(task, 2)

    async def test_mirror_cannot_negotiate_provider_cancellation(self):
        gate = Gate()
        features = Capabilities(audio_output=True)
        task = asyncio.create_task(AvipSession(gate, SessionLimits(), features).run())
        try:
            async with asyncio.timeout(2):
                start = _start()
                start["capabilities"].update(audio_output=True, response_cancel=True)
                await gate.inbox.put(("text", start))
                self.assertNotIn("response_cancel", (await gate.next_item())["capabilities"])
                await gate.inbox.put(("text", {"protocol": "avip/1", "type": "response.cancel", "response_id": 0}))
                self.assertEqual((await gate.next_item())["code"], "unsupported_message_type")
        finally:
            await gate.inbox.put(None)
            await asyncio.wait_for(task, 2)
