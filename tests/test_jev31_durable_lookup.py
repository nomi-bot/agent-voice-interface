"""Consume the JEV-28 durable run proposal over a loopback fixture."""

import json
import unittest

from websockets.asyncio.server import serve

from avip_client import AvipError
from avip_client.durable_result import list_recent_runs, lookup_run_result


TOKEN = "0" * 43
RUN_ID = "run-1"


def message(kind, **fields):
    return {"protocol": "avip/1", "type": kind, **fields}


class DurableLookupTests(unittest.IsolatedAsyncioTestCase):
    async def lookup(self, *, negotiated=True, denied=False, result_id=RUN_ID):
        seen = []

        async def gateway(socket):
            async for raw in socket:
                request = json.loads(raw)
                seen.append(request)
                if request["type"] == "session.start":
                    reply = message(
                        "session.ready", session_id="session-1", request_id=request["request_id"],
                        capabilities={"durable_runs": negotiated},
                    )
                elif request["type"] == "agent.run.authenticate":
                    reply = (
                        message("error", code="access_denied", field=None)
                        if denied else message("agent.run.authenticated")
                    )
                else:
                    reply = message(
                        "agent.run.result", status={"run_id": result_id, "state": "completed"},
                        retained=True, result="synthetic answer",
                    )
                await socket.send(json.dumps(reply))

        async with serve(gateway, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            try:
                result = await lookup_run_result(
                    f"ws://127.0.0.1:{port}/ws/avip", run_id=RUN_ID, token=TOKEN,
                )
            except AvipError as error:
                result = error.code
        return seen, result

    async def discover(self, rows, *, enabled=True):
        seen = []

        async def gateway(socket):
            async for raw in socket:
                request = json.loads(raw)
                seen.append(request)
                if request["type"] == "session.start":
                    reply = message(
                        "session.ready", session_id="session-1", request_id=request["request_id"],
                        capabilities={"durable_runs": True, "durable_run_discovery": enabled},
                    )
                elif request["type"] == "agent.run.authenticate":
                    reply = message("agent.run.authenticated")
                else:
                    reply = message("agent.run.recent", runs=rows)
                await socket.send(json.dumps(reply))

        async with serve(gateway, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            try:
                result = await list_recent_runs(f"ws://127.0.0.1:{port}/ws/avip", token=TOKEN)
            except AvipError as error:
                result = error.code
        return seen, result

    async def test_recent_fetches_without_a_known_id_or_owner_field(self):
        rows = [{"run_id": RUN_ID, "state": "completed"}]
        seen, discovered = await self.discover(rows)
        self.assertEqual([vars(run) for run in discovered], rows)
        self.assertEqual(seen[0]["capabilities"], {"durable_runs": True, "durable_run_discovery": True})
        self.assertEqual(seen[1:], [message("agent.run.authenticate", token=TOKEN), message("agent.run.recent")])

    async def test_recent_refuses_unnegotiated_discovery_before_sending_bearer(self):
        seen, result = await self.discover([], enabled=False)
        self.assertEqual(result, "durable_run_discovery_not_negotiated")
        self.assertEqual([frame["type"] for frame in seen], ["session.start"])

    async def test_recent_rejects_malformed_or_overlong_index(self):
        row = {"run_id": RUN_ID, "state": "running"}
        for rows in ([row, row], [row] * 17, [dict(row, result="private")], [dict(row, state="unknown")]):
            with self.subTest(rows=len(rows)):
                _, result = await self.discover(rows)
                self.assertEqual(result, "invalid_recent_runs")

    async def test_negotiated_lookup_authenticates_before_reading(self):
        received, result = await self.lookup()
        self.assertEqual(received[0]["capabilities"], {"durable_runs": True})
        self.assertEqual(received[1:], [
            message("agent.run.authenticate", token=TOKEN),
            message("agent.run.result", run_id=RUN_ID),
        ])
        self.assertEqual((result.run_id, result.state, result.result), (RUN_ID, "completed", "synthetic answer"))

    async def test_unnegotiated_server_receives_no_bearer(self):
        received, result = await self.lookup(negotiated=False)
        self.assertEqual(result, "durable_runs_not_negotiated")
        self.assertEqual([frame["type"] for frame in received], ["session.start"])

    async def test_denied_grant_never_issues_result_read(self):
        received, result = await self.lookup(denied=True)
        self.assertEqual(result, "access_denied")
        self.assertEqual([frame["type"] for frame in received], ["session.start", "agent.run.authenticate"])

    async def test_wrong_run_response_is_rejected(self):
        _, result = await self.lookup(result_id="run-other")
        self.assertEqual(result, "invalid_run_result")

    async def test_bearer_requires_a_numeric_loopback_avip_endpoint(self):
        with self.assertRaises(AvipError) as error:
            await lookup_run_result("ws://192.0.2.1:8000/ws/avip", run_id=RUN_ID, token=TOKEN)
        self.assertEqual(error.exception.code, "invalid_run_result_endpoint")


if __name__ == "__main__":
    unittest.main()
