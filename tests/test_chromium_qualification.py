"""Optional Chromium acceptance with synthetic media devices and loopback sockets."""
from contextlib import asynccontextmanager
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8000"


def serve_fixture(directory, listener_fd):
    import uvicorn

    from avi.core.durable_store import Observation
    from avi.core.lifecycle import AgentRunState
    from avi.local_gateway import open_runtime
    from avi.main import app
    from test_jev28_durable_runs import Backend

    class SyntheticBackend(Backend):
        async def submit(self, owner, *, kind, arguments, correlation_id):
            return Observation(correlation_id, AgentRunState.COMPLETED, result="Synthetic answer.")

        async def close(self):
            pass

    base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with base_lifespan(application), open_runtime(
            directory, backend=SyntheticBackend(), owner="browser-qa", scope="browser-qa",
            queries=["demo"], read_only=False, grant_seconds=180,
        ) as runtime:
            application.state.durable_runtime = runtime
            token = (directory / "client.grant").read_text().strip()
            principal = runtime.credentials.authenticate(token)
            await runtime.runs.submit(principal, "seed", "read_only", {"query": "demo"})
            yield

    app.router.lifespan_context = lifespan
    uvicorn.run(app, fd=listener_fd, proxy_headers=False, access_log=False, log_level="warning")


@unittest.skipUnless(os.environ.get("AVI_TEST_CHROMIUM"), "set AVI_TEST_CHROMIUM for the browser gate")
class ChromiumQualification(unittest.TestCase):
    def test_synthetic_voice_and_durable_run_recovery(self):
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as temp, socket.socket() as listener:
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", 8000))
                listener.listen()
            except OSError as exc:
                self.fail(f"Chromium qualification needs loopback port 8000: {exc}")
            directory = Path(temp)
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--fixture", temp, str(listener.fileno())],
                cwd=ROOT, env={"PATH": os.defpath, "AVIP_MOCK_MEDIA": "1", "PYTHONPATH": str(ROOT / "src")},
                pass_fds=(listener.fileno(),),
            )
            try:
                opener = build_opener(ProxyHandler({}))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail("Fixture failed to start")
                    try:
                        with opener.open(f"{ORIGIN}/health", timeout=0.5) as response:
                            if json.load(response).get("agent") == "durable" and (directory / "client.grant").exists():
                                break
                    except OSError:
                        pass
                    time.sleep(0.05)
                else:
                    self.fail("Fixture failed readiness")
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        executable_path=os.environ["AVI_TEST_CHROMIUM"],
                        args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                              "--autoplay-policy=no-user-gesture-required"],
                    )
                    try:
                        self.exercise(browser, (directory / "client.grant").read_text().strip())
                    finally:
                        browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def exercise(self, browser, grant):
        context = browser.new_context(permissions=["microphone"])
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{ORIGIN}/voice")
        page.locator("#runs summary").click()

        password = page.locator("#run-grant")
        password.fill(grant)
        page.locator("#run-recent").click()
        page.wait_for_function("document.querySelector('#run-list').length > 1")
        self.assertEqual(password.input_value(), "")
        page.locator("#run-list").select_option(index=1)
        password.fill(grant)
        page.locator("#run-result").click()
        page.wait_for_function("document.querySelector('#run-output').textContent.includes('Synthetic answer.')")
        password.fill(grant)
        page.locator("#run-attach").click()
        page.wait_for_function("Number(document.querySelector('#run-cursor').value) > 0")

        password.fill(grant)
        page.locator("#voice-tasks").check()
        page.locator("#start").click()
        page.wait_for_function("Number(document.querySelector('#played').textContent) >= 8")
        uplink_before_stop = int(page.locator("#uplink").inner_text())
        self.assertGreaterEqual(int(page.locator("#downlink").inner_text()), 8)
        page.locator("#stop-playback").click()
        page.wait_for_function("n => Number(document.querySelector('#uplink').textContent) > n + 5",
                               arg=uplink_before_stop)
        page.locator("#end").click()
        page.wait_for_function("!document.querySelector('#start').disabled")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fixture"]:
        serve_fixture(Path(sys.argv[2]), int(sys.argv[3]))
    else:
        unittest.main()
