"""Run the browser duplex fault suite under the regular test entry point."""

from pathlib import Path
import subprocess
import unittest


DUPLEX_CHECK = Path(__file__).resolve().parents[1] / "clients" / "browser" / "duplex-check.mjs"


class BrowserDuplexTests(unittest.TestCase):
    def test_duplex_fault_scenarios(self):
        completed = subprocess.run(
            ["node", "--test", str(DUPLEX_CHECK)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
