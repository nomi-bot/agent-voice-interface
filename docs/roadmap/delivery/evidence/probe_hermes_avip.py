"""Exercise two real loopback gateways with a deterministic model and no microphone."""

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import tempfile
import time

from avip_client.errors import AvipError
from avip_client.session import AvipClient
import probe_hermes_runs as upstream


URL = "ws://127.0.0.1:8000/ws/avip"
SOURCE = Path(__file__).resolve().parents[4]


@contextmanager
def avi_gateway(python, state, origin, key, *, read_only=False):
    # Fail if another local process owns the public CLI's fixed port.
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        check.bind(("127.0.0.1", 8000))
    options = ["--read-only"] if read_only else ["--allow-query", upstream.CANCEL]
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(SOURCE / "src"),
        "HERMES_API_KEY": key,
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen([
        str(python.absolute()), "-m", "avi.local_gateway", "--state-dir", str(state),
        "--hermes-origin", origin, "--backend-scope", "probe-local-scope",
        "--owner", "probe-owner", *options,
    ], env=env, cwd=state.parent)
    try:
        def ready():
            grant = state / "client.grant"
            if not grant.exists():
                return None
            try:
                with socket.create_connection(("127.0.0.1", 8000), timeout=0.25):
                    return grant.read_text().strip()
            except OSError:
                return None

        yield upstream.wait_for(ready, "AVI grant and listener", process)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@asynccontextmanager
async def authenticated(token):
    async with AvipClient(URL, request_id="test-start", device_id="test-robot",
                          capabilities={"durable_runs": True}) as client:
        ready = await client.start()
        assert ready["capabilities"].get("durable_runs") is True
        await send(client, "authenticate", token=token)
        assert (await client.recv())["type"] == "agent.run.authenticated"
        yield client


async def send(client, operation, **fields):
    await client.send_control({"protocol": "avip/1", "type": f"agent.run.{operation}", **fields})


async def accepted(token, model):
    async with asyncio.timeout(30), authenticated(token) as client:
        fields = {"request_id": "test-submit", "kind": "read_only", "arguments": {"query": upstream.CANCEL}}
        await send(client, "submit", **fields)
        receipt = await client.recv()
        assert receipt["type"] == "agent.run.status"
        run_id = receipt["run_id"]
        await send(client, "submit", **fields)
        assert (await client.recv())["run_id"] == run_id
        assert await asyncio.to_thread(model.entered[upstream.CANCEL].wait, 15)
        return run_id, receipt["cursor"]


async def recovered(token, old_token, run_id, after_cursor):
    async with asyncio.timeout(30):
        try:
            async with authenticated(old_token):
                raise AssertionError("A pre-restart grant remained valid")
        except AvipError as exc:
            assert exc.code == "access_denied"
        async with authenticated(token) as client:
            while True:
                await send(client, "result", run_id=run_id)
                result = await client.recv()
                assert result["type"] == "agent.run.result"
                if result["status"]["state"] == "completed":
                    assert result["retained"] and result["result"] == "AVI_JEV26_OK"
                    break
                await asyncio.sleep(0.25)
            await send(client, "attach", run_id=run_id, after_cursor=after_cursor)
            cursors = []
            terminal = False
            while True:
                event = await client.recv()
                assert event["run_id"] == run_id
                if event["type"] == "agent.run.replay":
                    assert cursors and event["resume_cursor"] == cursors[-1]
                    assert terminal
                    return len(cursors)
                assert event["type"] == "agent.run.event"
                assert event["cursor"] == (cursors[-1] if cursors else after_cursor) + 1
                cursors.append(event["cursor"])
                terminal |= event["kind"] == "completed" and event["payload"].get("result") == "AVI_JEV26_OK"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path, help="Pinned upstream Hermes checkout")
    parser.add_argument("--avi-python", required=True, type=Path, help="Python with AVI development extras")
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    pin = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if pin != upstream.HERMES_COMMIT or platform.python_version() != "3.14.10":
        parser.error("Use ADR-012's upstream checkout and its Python 3.14.10 interpreter")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    key = secrets.token_urlsafe(32)
    start = time.time()
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="avi-probe-") as tmp, upstream.model_server() as model:
        state = Path(tmp)
        with upstream.gateway(checkout, state / "hermes", model, port, key) as (_, _, caps):
            assert caps["features"]["runs_idempotency"]["durable"]
            with avi_gateway(args.avi_python, state / "avi", origin, key) as old_token:
                run_id, cursor = asyncio.run(accepted(old_token, model))
            model.release[upstream.CANCEL].set()
            with avi_gateway(args.avi_python, state / "avi", origin, key, read_only=True) as token:
                count = asyncio.run(recovered(token, old_token, run_id, cursor))
            assert model.calls == {upstream.CANCEL: 1}
            assert model.tool_counts == {upstream.CANCEL: 0}
    print(json.dumps({
        "avi_revision": subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip(),
        "hermes_revision": pin, "hermes_python": platform.python_version(),
        "model": "deterministic loopback", "transport": "real AVIP WebSocket and Hermes HTTP",
        "attempted_submits": 2, "remote_executions": 1, "model_tools": 0,
        "retained_result_matched": True, "replayed_events": count, "stale_grant": "denied",
        "gateway_restart": "read-only recovery", "seconds": round(time.time() - start, 3),
    }, indent=2))


if __name__ == "__main__":
    main()
