import json
from pathlib import Path
import os
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith(("linux", "darwin")), "POSIX gateway runner")
class MockSocketSoakTests(unittest.TestCase):
    def test_two_clients_validate_media_across_reconnect(self):
        process = subprocess.run(
            [sys.executable, "scripts/mock_socket_soak.py", "--seconds", "1",
             "--clients", "2", "--reconnect-seconds", "0.3"],
            cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True, text=True, timeout=25,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        report = json.loads(process.stdout)
        self.assertTrue(report["passed"])
        self.assertEqual(report["dropped_or_unverified_frames"], 0)
        for client in report["clients"]:
            self.assertGreater(client["connections"], 1)
            self.assertGreater(client["verified_frames"], 1)
            self.assertEqual(client["verified_frames"], client["sent_frames"])
