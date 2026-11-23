import unittest

import httpx

from avi.agents.hermes import (
    HermesAgentAdapter,
    HermesError,
    HermesErrorCode,
    HermesExecutionContext,
    HermesTask,
)
from avi.agents.hermes_http import HermesHttpTransport
from avi.core.lifecycle import AgentRunState as State


LOCAL_URL = "http://127.0.0.1:8642"
TEST_KEY = "local-placeholder-key"
CONTEXT = HermesExecutionContext("owner-a")
TASK = HermesTask("read_only", {"query": "Return the harmless loopback greeting."})
CORRELATION = "avi-correlation-1"
HANDLE = "run_0123456789abcdef0123456789abcdef"


class HermesHttpTests(unittest.IsolatedAsyncioTestCase):
    def adapter_for(self, responder, **options):
        transport = HermesHttpTransport(
            LOCAL_URL, TEST_KEY, transport=httpx.MockTransport(responder), **options,
        )
        adapter = HermesAgentAdapter(transport)
        self.addAsyncCleanup(adapter.close)
        return adapter

    async def test_post_then_get_uses_pinned_shapes_and_durable_key(self):
        requests = []

        async def handle(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(202, json={"run_id": HANDLE, "status": "started", "replayed": False})
            return httpx.Response(
                200, json={"object": "hermes.run", "run_id": HANDLE,
                           "status": "completed", "output": "loopback greeting"},
            )

        adapter = self.adapter_for(handle)
        admission = await adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
        self.assertEqual(admission.state, State.SUBMITTED)
        result = await adapter.status(admission.handle, CONTEXT, previous=admission)
        self.assertEqual(result.state, State.COMPLETED)
        self.assertEqual(result.result, "loopback greeting")
        self.assertEqual([(req.method, req.url.path) for req in requests], [
            ("POST", "/v1/runs"), ("GET", f"/v1/runs/{HANDLE}"),
        ])
        self.assertEqual(requests[0].headers["Idempotency-Key"], CORRELATION)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer " + TEST_KEY)
        self.assertEqual(requests[0].content, b'{"input":"Return the harmless loopback greeting."}')
        self.assertFalse(adapter.capabilities.progress)
        self.assertFalse(adapter.capabilities.correlation_lookup)

    async def test_invalid_task_never_sends_an_upstream_body(self):
        def no_request(request):
            self.fail("task reached upstream")

        adapter = self.adapter_for(no_request)
        for task in (
            HermesTask("unsupported", {"query": "greeting"}),
            HermesTask("read_only", {"query": "greeting", "session_id": "private"}),
            HermesTask("read_only", {"query": "x" * 4097}),
            HermesTask("read_only", {"query": "  "}),
        ):
            with self.assertRaises(HermesError) as raised:
                await adapter.submit(task, CONTEXT, correlation_id=CORRELATION)
            self.assertEqual(raised.exception.code, HermesErrorCode.INVALID_REQUEST)

    async def test_accepted_202_without_expected_handle_stays_uncertain(self):
        for payload in (
            {"status": "started"},
            {"run_id": "should-not-be-used/as-a-path", "status": "started"},
            {"run_id": HANDLE, "status": ["started"]},
        ):
            adapter = self.adapter_for(lambda _: httpx.Response(202, json=payload))
            with self.assertRaises(HermesError) as raised:
                await adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
            self.assertEqual(raised.exception.outcome, "uncertain")
            self.assertEqual(raised.exception.correlation_id, CORRELATION)
            self.assertNotIn("should-not-be-used", str(raised.exception))

    async def test_terminal_admission_replay_still_reads_result(self):
        adapter = self.adapter_for(
            lambda request: httpx.Response(
                202 if request.method == "POST" else 200,
                json={"run_id": HANDLE, "status": "completed", "replayed": True}
                if request.method == "POST" else
                {"object": "hermes.run", "run_id": HANDLE, "status": "completed", "output": "greeting"},
            ),
        )
        admission = await adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
        self.assertEqual(admission.state, State.SUBMITTED)
        self.assertEqual((await adapter.status(HANDLE, CONTEXT, previous=admission)).result, "greeting")

    async def test_status_classifies_approval_wait_stopping_and_cancellation(self):
        replies = iter((
            {"object": "hermes.run", "run_id": HANDLE, "status": "waiting_for_approval",
             "pending_controls": [{"prompt_id": "private-prompt"}]},
            {"run_id": HANDLE, "status": "stopping"},
            {"object": "hermes.run", "run_id": HANDLE, "status": "cancelled", "output": "private diagnostic"},
        ))
        requests = []

        async def handle(request):
            requests.append(request)
            return httpx.Response(200, json=next(replies))

        adapter = self.adapter_for(handle)
        observed = await adapter.status(HANDLE, CONTEXT)
        self.assertEqual(observed.state, State.RUNNING)
        self.assertIsNone(await adapter.cancel(HANDLE, CONTEXT, previous=observed))
        terminal = await adapter.status(HANDLE, CONTEXT, previous=observed)
        self.assertEqual(terminal.state, State.CANCELLED)
        self.assertIsNone(terminal.result)
        self.assertEqual([(req.method, req.url.path) for req in requests], [
            ("GET", f"/v1/runs/{HANDLE}"),
            ("POST", f"/v1/runs/{HANDLE}/stop"),
            ("GET", f"/v1/runs/{HANDLE}"),
        ])

    async def test_denials_map_to_safe_codes_and_have_no_fallback(self):
        for status, remote_code, expected in (
            (401, "gateway_auth_failed", HermesErrorCode.CREDENTIALS_REJECTED),
            (409, "permission_denied", HermesErrorCode.APPROVAL_DENIED),
        ):
            calls = []

            async def handle(request):
                calls.append(request)
                return httpx.Response(status, json={"error": {"message": "private", "code": remote_code}})

            adapter = self.adapter_for(handle)
            with self.assertRaises(HermesError) as raised:
                await adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
            self.assertEqual(raised.exception.outcome, "denied")
            self.assertEqual(raised.exception.code, expected)
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(len(calls), 1)

    async def test_exact_idempotency_conflict_is_a_rejection(self):
        for remote_code, expected in (
            ("idempotency_key_conflict", HermesErrorCode.REQUEST_REJECTED),
            ("unknown_conflict", HermesErrorCode.SUBMIT_UNCERTAIN),
        ):
            with self.subTest(code=remote_code):
                adapter = self.adapter_for(lambda _: httpx.Response(
                    409, json={"error": {"code": remote_code, "message": "private"}},
                ))
                with self.assertRaises(HermesError) as raised:
                    await adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
                self.assertEqual(raised.exception.code, expected)
                self.assertEqual(raised.exception.correlation_id, CORRELATION)

    async def test_recovery_never_uses_submit_and_preserves_unknown(self):
        requests = []

        async def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"object": "hermes.run", "run_id": HANDLE, "status": "unknown"})

        adapter = self.adapter_for(handle)
        self.assertIsNone(await adapter.reconcile(CONTEXT, correlation_id=CORRELATION))
        self.assertEqual(requests, [])
        self.assertIsNone(await adapter.reconcile(CONTEXT, correlation_id=CORRELATION, handle=HANDLE))
        self.assertEqual([(req.method, req.url.path) for req in requests], [
            ("GET", f"/v1/runs/{HANDLE}"),
        ])

    async def test_oversized_wire_and_result_have_safe_errors(self):
        for output in ("x" * 4097, "x" * (128 * 1024)):
            adapter = self.adapter_for(lambda _: httpx.Response(
                200, json={"object": "hermes.run", "run_id": HANDLE, "status": "completed", "output": output},
            ))
            with self.assertRaises(HermesError) as raised:
                await adapter.status(HANDLE, CONTEXT)
            self.assertEqual(raised.exception.code, HermesErrorCode.INVALID_RESPONSE)

    async def test_redirect_is_never_followed(self):
        requests = []

        async def handle(request):
            requests.append(request)
            return httpx.Response(307, headers={"Location": "https://other.invalid/v1/runs"})

        adapter = self.adapter_for(handle)
        with self.assertRaises(HermesError) as raised:
            await adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
        self.assertEqual(raised.exception.code, HermesErrorCode.SUBMIT_UNCERTAIN)
        self.assertEqual(len(requests), 1)

    def test_config_requires_numeric_loopback_and_a_server_credential(self):
        for base, key in (
            ("http://gateway.example", TEST_KEY),
            ("http://127.0.0.1.example", TEST_KEY),
            ("http://alice@127.0.0.1", TEST_KEY),
            ("http://127.0.0.1/v1", TEST_KEY),
            ("http://127.0.0.1:invalid", TEST_KEY),
            (LOCAL_URL, ""),
            (LOCAL_URL, "secret\nfield"),
        ):
            with self.subTest(base=base), self.assertRaises(ValueError):
                HermesHttpTransport(base, key)
