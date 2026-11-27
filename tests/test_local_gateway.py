from pathlib import Path
import stat
import tempfile
import unittest

from avi.core.durable_auth import AccessDenied, RunPermission
from avi.core.durable_store import Observation
from avi.core.lifecycle import AgentRunState
from avi.local_gateway import open_runtime


class Backend:
    cancellation = False
    closed = False

    async def submit(self, owner, *, kind, arguments, correlation_id):
        return Observation("test-handle", AgentRunState.RUNNING)

    async def reconcile(self, owner, *, correlation_id, handle):
        return Observation(handle, AgentRunState.COMPLETED, result="synthetic result")

    async def close(self):
        self.closed = True


class LocalGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.grant = self.state / "client.grant"

    def runtime(self, backend, *, read_only=False):
        return open_runtime(self.state, backend=backend, owner="operator", scope="local-v1",
                            queries=["approved"], read_only=read_only, grant_seconds=60)

    async def test_private_grant_rotation_and_result_recovery_in_read_only_mode(self):
        original = Backend()
        async with self.runtime(original) as first:
            token = self.grant.read_text().strip()
            self.assertEqual(stat.S_IMODE(self.grant.stat().st_mode), 0o600)
            connection = first.credentials.authenticate(token)
            self.assertTrue(first.approve_query(connection, "approved"))
            self.assertFalse(first.approve_query(connection, "other"))
            run = await first.runs.submit(connection, "req-1", "read_only", {"query": "approved"})
            busy = Backend()
            with self.assertRaisesRegex(ValueError, "active gateway"):
                async with self.runtime(busy):
                    self.fail("second writer admitted")
            self.assertTrue(busy.closed)
            self.assertEqual(self.grant.read_text().strip(), token)
        self.assertTrue(original.closed)
        self.assertFalse(self.grant.exists())
        with self.assertRaises(AccessDenied):
            first.credentials.require(connection)
        async with self.runtime(Backend(), read_only=True) as second:
            self.assertFalse(second.cutover.durable_enabled)
            with self.assertRaises(AccessDenied):
                second.credentials.authenticate(token)
            restored = second.credentials.authenticate(self.grant.read_text().strip())
            self.assertEqual((await second.runs.result(restored, run.run_id)).result, "synthetic result")
            for permission in (RunPermission.SUBMIT, RunPermission.CANCEL):
                with self.assertRaises(AccessDenied):
                    second.credentials.require(restored, permission)

    async def test_shared_directory_rejected_before_issuing_grant(self):
        self.state.chmod(0o755)
        backend = Backend()
        with self.assertRaisesRegex(ValueError, "private"):
            async with self.runtime(backend):
                self.fail("shared state opened")
        self.assertFalse(self.grant.exists())
        self.assertTrue(backend.closed)
