import asyncio
import unittest

from avi.agents.hermes import (
    HermesAgentAdapter,
    HermesCapabilities,
    HermesError,
    HermesErrorCode,
    HermesExecutionContext,
    HermesRun,
    HermesTask,
)
from avi.core.lifecycle import AgentRunState as State


CONTEXT = HermesExecutionContext("owner-a")
TASK = HermesTask("read_only", {"query": "summarize the test greeting"})
CORRELATION = "avi-request-1"
HANDLE = "remote-run-1"


class FakeHermesTransport:
    capabilities = HermesCapabilities(
        status=True, progress=True, cancellation=True, correlation_lookup=True,
    )

    def __init__(self):
        self.submissions = []
        self.requests = []
        self.submission_error = None
        self.cancel_reply = None
        self.report = HermesRun(HANDLE, State.RUNNING)
        self.lookup_reply = None
        self.closed = 0

    async def submit(self, task, context, correlation_id):
        self.submissions.append((task, context, correlation_id))
        if self.submission_error:
            raise self.submission_error
        return self.report

    async def status(self, handle, context):
        self.requests.append(("status", handle, context))
        return self.report

    async def lookup(self, correlation_id, context):
        self.requests.append(("lookup", correlation_id, context))
        return self.lookup_reply

    async def cancel(self, handle, context):
        self.requests.append(("cancel", handle, context))
        return self.cancel_reply

    async def close(self):
        self.closed += 1


class HermesAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.transport = FakeHermesTransport()
        self.adapter = HermesAgentAdapter(self.transport)
        self.addAsyncCleanup(self.adapter.close)

    async def test_submit_and_reconcile_existing_handle(self):
        run = await self.adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
        self.assertEqual(run.state, State.RUNNING)
        self.assertEqual(self.transport.submissions, [(TASK, CONTEXT, CORRELATION)])
        self.transport.report = HermesRun(HANDLE, State.COMPLETED, result="greeting")

        settled = await self.adapter.reconcile(
            CONTEXT, correlation_id=CORRELATION, handle=run.handle, previous=run,
        )
        self.assertEqual(settled, self.transport.report)
        self.assertEqual(self.transport.requests, [("status", HANDLE, CONTEXT)])

    async def test_transport_approval_and_credential_denials_are_typed(self):
        for code in (HermesErrorCode.APPROVAL_DENIED, HermesErrorCode.CREDENTIALS_REJECTED):
            with self.subTest(code=code):
                self.transport.submission_error = HermesError(code)
                with self.assertRaises(HermesError) as raised:
                    await self.adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
                self.assertEqual(raised.exception.outcome, "denied")
                self.assertEqual(raised.exception.code, code)
        self.assertEqual(len(self.transport.submissions), 2)
        self.assertEqual(self.transport.requests, [])

    async def test_post_accept_disconnect_is_uncertain_and_does_not_resubmit(self):
        self.transport.submission_error = OSError("upstream-private-handle")
        with self.assertRaises(HermesError) as raised:
            await self.adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
        self.assertEqual(raised.exception.outcome, "uncertain")
        self.assertEqual(raised.exception.correlation_id, CORRELATION)
        self.assertNotIn("upstream-private-handle", str(raised.exception))
        self.assertEqual(len(self.transport.submissions), 1)

        self.assertIsNone(await self.adapter.reconcile(CONTEXT, correlation_id=CORRELATION))
        self.transport.lookup_reply = self.transport.report
        run = await self.adapter.reconcile(CONTEXT, correlation_id=CORRELATION)
        self.assertEqual(run.handle, HANDLE)
        self.assertEqual(len(self.transport.submissions), 1)

    async def test_unavailable_capabilities_do_not_call_transport(self):
        self.transport.capabilities = HermesCapabilities()
        adapter = HermesAgentAdapter(self.transport)
        self.addAsyncCleanup(adapter.close)
        for operation in (adapter.status, adapter.progress, adapter.cancel):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(HermesError) as raised:
                    await operation(HANDLE, CONTEXT)
                self.assertEqual(raised.exception.outcome, "unsupported")
        self.assertIsNone(await adapter.reconcile(CONTEXT, correlation_id=CORRELATION))
        self.assertEqual(self.transport.requests, [])

    async def test_cancel_acknowledgement_keeps_run_active(self):
        active = await self.adapter.status(HANDLE, CONTEXT)
        self.assertIsNone(await self.adapter.cancel(HANDLE, CONTEXT, previous=active))
        self.assertEqual(await self.adapter.status(HANDLE, CONTEXT), active)

        self.transport.report = HermesRun(HANDLE, State.CANCELLED)
        self.assertEqual(
            (await self.adapter.status(HANDLE, CONTEXT, previous=active)).state,
            State.CANCELLED,
        )

    async def test_repeated_and_out_of_order_reports_preserve_last_confirmed_state(self):
        active = self.transport.report
        self.transport.report = HermesRun(HANDLE, State.QUEUED)
        self.assertEqual(await self.adapter.status(HANDLE, CONTEXT, previous=active), active)

        settled = HermesRun(HANDLE, State.COMPLETED, result="first result")
        for late in (
            settled,
            HermesRun(HANDLE, State.COMPLETED, result="different result"),
            HermesRun(HANDLE, State.FAILED),
            active,
        ):
            self.transport.report = late
            self.assertEqual(await self.adapter.status(HANDLE, CONTEXT, previous=settled), settled)

    async def test_malformed_report_does_not_replace_previous_state(self):
        active = self.transport.report
        for report in (
            {"status": "completed"},
            HermesRun("another-run", State.COMPLETED, result="other owner's result"),
            HermesRun(HANDLE, "wrong"),
            HermesRun(HANDLE, State.COMPLETED, result="private" * 600),
            HermesRun(HANDLE, State.RUNNING, result="premature result"),
        ):
            self.transport.report = report
            with self.assertRaises(HermesError) as raised:
                await self.adapter.status(HANDLE, CONTEXT, previous=active)
            self.assertEqual(raised.exception.code, HermesErrorCode.INVALID_RESPONSE)
            self.assertNotIn("private", str(raised.exception))
            self.assertEqual(active, HermesRun(HANDLE, State.RUNNING))

    async def test_oversized_acceptance_response_is_uncertain(self):
        self.transport.report = HermesRun(HANDLE, State.COMPLETED, result="x" * 4097)
        with self.assertRaises(HermesError) as raised:
            await self.adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
        self.assertEqual(raised.exception.outcome, "uncertain")
        self.assertEqual(raised.exception.correlation_id, CORRELATION)

    async def test_progress_returns_only_verified_snapshot_data(self):
        self.assertIsNone(await self.adapter.progress(HANDLE, CONTEXT))
        self.transport.report = HermesRun(HANDLE, State.RUNNING, progress="reading")
        self.assertEqual(await self.adapter.progress(HANDLE, CONTEXT), "reading")

    async def test_close_twice_releases_transport_without_cancel(self):
        await self.adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
        await asyncio.gather(self.adapter.close(), self.adapter.close())
        self.assertEqual(self.transport.closed, 1)
        self.assertEqual(self.transport.requests, [])
        with self.assertRaises(HermesError) as raised:
            await self.adapter.submit(TASK, CONTEXT, correlation_id="next-request")
        self.assertEqual(raised.exception.code, HermesErrorCode.CLOSED)
        self.assertEqual(len(self.transport.submissions), 1)

    async def test_close_during_submit_preserves_uncertain_correlation(self):
        started = asyncio.Event()
        released = asyncio.Event()

        class PendingTransport(FakeHermesTransport):
            async def submit(self, task, context, correlation_id):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    released.set()

        transport = PendingTransport()
        adapter = HermesAgentAdapter(transport)
        self.addAsyncCleanup(adapter.close)
        submission = asyncio.create_task(adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION))
        await asyncio.wait_for(started.wait(), 1)
        await adapter.close()
        with self.assertRaises(HermesError) as raised:
            await submission
        self.assertEqual(raised.exception.outcome, "uncertain")
        self.assertEqual(raised.exception.correlation_id, CORRELATION)
        self.assertTrue(released.is_set())
        self.assertEqual(transport.requests, [])

    async def test_submit_timeout_is_uncertain(self):
        class StalledTransport(FakeHermesTransport):
            async def submit(self, task, context, correlation_id):
                await asyncio.Future()

        adapter = HermesAgentAdapter(StalledTransport(), timeout=0.02)
        self.addAsyncCleanup(adapter.close)
        with self.assertRaises(HermesError) as raised:
            await adapter.submit(TASK, CONTEXT, correlation_id=CORRELATION)
        self.assertEqual(raised.exception.outcome, "uncertain")
        self.assertEqual(raised.exception.correlation_id, CORRELATION)

    async def test_cancel_rejects_preceding_evidence_for_a_different_handle(self):
        other = HermesRun("another-run", State.RUNNING)
        with self.assertRaises(HermesError) as raised:
            await self.adapter.cancel(HANDLE, CONTEXT, previous=other)
        self.assertEqual(raised.exception.code, HermesErrorCode.INVALID_REQUEST)
        self.assertEqual(self.transport.requests, [])
