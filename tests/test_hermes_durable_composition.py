"""Exercise the real service and adapter with SQLite and an in-process HTTP fake."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from avi.agents.hermes import HermesAgentAdapter
from avi.agents.hermes_durable import HermesDurableBackend
from avi.agents.hermes_http import HermesHttpTransport
from avi.core.durable_auth import AccessDenied, LoopbackCredentials, RunPermission
from avi.core.durable_runs import DurableRunService
from avi.core.durable_store import DispatchState, RunConflict, SQLiteRunStore
from avi.core.lifecycle import AgentRunState as State


HANDLE = "run_0123456789abcdef0123456789abcdef"
ORIGIN = "http://127.0.0.1:8642"
KEY = "in-process-test-bearer"
QUERY = "Return the fixed marker."
SCOPE = "test-hermes-profile"


class RecordingAdapter(HermesAgentAdapter):
    """Observe the owner crossing the internal boundary without exposing it on HTTP."""

    def __init__(self, transport):
        super().__init__(transport)
        self.calls = []

    async def submit(self, task, context, *, correlation_id):
        self.calls.append(("submit", context.principal_id, correlation_id))
        return await super().submit(task, context, correlation_id=correlation_id)

    async def reconcile(self, context, *, correlation_id, handle=None, previous=None):
        self.calls.append(("reconcile", context.principal_id, correlation_id))
        return await super().reconcile(
            context, correlation_id=correlation_id, handle=handle, previous=previous,
        )

    async def cancel(self, handle, context, *, previous=None):
        self.calls.append(("cancel", context.principal_id, None))
        return await super().cancel(handle, context, previous=previous)


class HermesDurableCompositionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Path(directory) / "runs.sqlite"
        self.credentials = LoopbackCredentials()
        self.alice = self.grant("alice")
        self.bob = self.grant("bob")
        self.requests = []
        self.remote_state = "running"
        self.submit_reply = None
        self.transport_lost = False
        self.service, self.adapter = self.open_service()

    def grant(self, owner):
        return self.credentials.authenticate(self.credentials.issue(owner, RunPermission))

    def open_service(self, *, scope=SCOPE):
        adapter = RecordingAdapter(
            HermesHttpTransport(ORIGIN, KEY, transport=httpx.MockTransport(self.respond)),
        )
        backend = HermesDurableBackend(adapter)
        self.addAsyncCleanup(backend.close)
        return DurableRunService(
            SQLiteRunStore(self.db), self.credentials, backend,
            admissions_open=True, backend_scope=scope,
        ), adapter

    async def respond(self, request):
        self.assertEqual(request.headers["authorization"], "Bearer " + KEY)
        self.assertEqual(request.url.host, "127.0.0.1")
        self.requests.append((request.method, request.url.path, request.headers.get("idempotency-key")))
        if (request.method, request.url.path) == ("POST", "/v1/runs"):
            self.assertEqual(json.loads(request.content), {"input": QUERY})
            if self.transport_lost:
                raise httpx.ReadError("private mock transport diagnostic", request=request)
            if self.submit_reply is not None:
                return self.submit_reply
            return httpx.Response(202, json={"run_id": HANDLE, "status": "started", "replayed": False})
        if (request.method, request.url.path) == ("GET", f"/v1/runs/{HANDLE}"):
            payload = {"object": "hermes.run", "run_id": HANDLE, "status": self.remote_state}
            if self.remote_state == "completed":
                payload["output"] = "fixture result"
            return httpx.Response(200, json=payload)
        if (request.method, request.url.path) == ("POST", f"/v1/runs/{HANDLE}/stop"):
            return httpx.Response(200, json={"run_id": HANDLE, "status": "stopping"})
        self.fail(f"Unexpected provider operation: {request.method} {request.url.path}")

    async def submit(self, *, service=None, request_id="request-1", query=QUERY):
        return await (service or self.service).submit(self.alice, request_id, "read_only", {"query": query})

    async def test_same_owner_conflict_and_reopen_known_result_without_duplicate_submit(self):
        second, next_adapter = self.open_service()
        one, two = await asyncio.gather(self.submit(), self.submit(service=second))
        self.assertEqual(one.run_id, two.run_id)
        self.assertEqual(self.requests, [("POST", "/v1/runs", one.run_id)])
        self.assertEqual(self.adapter.calls + next_adapter.calls, [("submit", "alice", one.run_id)])
        with self.assertRaises(RunConflict):
            await self.submit(service=second, query="A different local text.")
        for method in (second.status, second.result, second.attach, second.cancel):
            with self.assertRaises(AccessDenied):
                await method(self.bob, one.run_id)
        self.assertEqual(len(self.requests), 1)

        reopened, fresh_adapter = self.open_service()
        await reopened.recover(startup=False)
        active = await reopened.status(self.alice, one.run_id)
        self.assertEqual(active.state, State.RUNNING)
        self.remote_state = "completed"
        await reopened.recover(startup=False)
        new_connection = self.grant("alice")
        replay = await reopened.attach(new_connection, one.run_id, after_cursor=active.cursor)
        self.assertIsNone(replay.snapshot)
        self.assertEqual([(event.kind, event.payload) for event in replay.events], [
            ("completed", {"result": "fixture result"}),
        ])
        result = await reopened.result(new_connection, one.run_id)
        self.assertEqual(result.status.state, State.COMPLETED)
        self.assertEqual(result.result, "fixture result")
        self.assertEqual(replay.resume_cursor, result.status.cursor)
        self.assertNotIn(HANDLE, repr(replay))
        self.assertEqual(fresh_adapter.calls, [
            ("reconcile", "alice", one.run_id), ("reconcile", "alice", one.run_id),
        ])
        self.assertEqual([entry[0] for entry in self.requests], ["POST", "GET", "GET"])

    async def test_lost_receipt_and_malformed_admission_remain_uncertain_without_resubmit(self):
        for index, provider_lost_receipt in enumerate((True, False)):
            with self.subTest(lost=provider_lost_receipt):
                self.transport_lost = provider_lost_receipt
                self.submit_reply = httpx.Response(202, json={"status": "started"})
                request_id = f"uncertain-{index}"
                run = await self.submit(request_id=request_id)
                self.assertEqual(run.dispatch, DispatchState.UNCERTAIN)
                reopened, fresh_adapter = self.open_service()
                await reopened.recover()
                duplicate = await self.submit(service=reopened, request_id=request_id)
                self.assertEqual(duplicate.run_id, run.run_id)
                self.assertEqual(duplicate.dispatch, DispatchState.UNCERTAIN)
                self.assertTrue(duplicate.reconciliation_pending)
                self.assertEqual(fresh_adapter.calls, [])
        self.assertEqual([entry[0] for entry in self.requests], ["POST", "POST"])

    async def test_typed_credential_denial_conflict_and_opaque_conflict(self):
        examples = (
            (401, {"error": {"message": "private"}}, "dispatch_denied", DispatchState.CLOSED),
            (409, {"error": {"code": "idempotency_key_conflict"}}, "dispatch_failed", DispatchState.CLOSED),
            (409, {"error": {"code": "unknown_execution"}}, None, DispatchState.UNCERTAIN),
            (409, {"error": {"code": "unexpected_conflict"}}, None, DispatchState.UNCERTAIN),
        )
        for index, (status, body, failure, dispatch) in enumerate(examples):
            with self.subTest(status=status, body=body):
                self.submit_reply = httpx.Response(status, json=body)
                run = await self.submit(request_id=f"fixture-{index}")
                self.assertEqual((run.failure_code, run.dispatch), (failure, dispatch))

    async def test_cancel_acknowledgement_is_not_terminal_and_next_read_confirms(self):
        run = await self.submit()
        await self.service.recover(startup=False)
        stopped = await self.service.cancel(self.alice, run.run_id)
        self.assertEqual(stopped.state, State.RUNNING)
        self.assertEqual(self.adapter.calls[-1], ("cancel", "alice", None))
        self.remote_state = "cancelled"
        await self.service.recover(startup=False)
        result = await self.service.result(self.alice, run.run_id)
        self.assertEqual(result.status.state, State.CANCELLED)
        self.assertIsNone(result.result)
        self.assertEqual([entry[1] for entry in self.requests], [
            "/v1/runs", f"/v1/runs/{HANDLE}", f"/v1/runs/{HANDLE}/stop", f"/v1/runs/{HANDLE}",
        ])

    async def test_scope_change_does_not_poll_old_handle(self):
        run = await self.submit()
        changed, fresh_adapter = self.open_service(scope="different-profile")
        await changed.recover()
        current = await changed.status(self.alice, run.run_id)
        self.assertTrue(current.reconciliation_pending)
        self.assertEqual(fresh_adapter.calls, [])
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
