"""Run the AVI adapter against the pinned JEV-26 loopback daemon."""

import argparse
import asyncio
from contextlib import AsyncExitStack
import importlib.util
import json
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import tempfile

from avi.agents.hermes import (
    HermesAgentAdapter,
    HermesError,
    HermesErrorCode,
    HermesExecutionContext,
    HermesTask,
)
from avi.agents.hermes_http import HermesHttpTransport
from avi.core.lifecycle import AgentRunState


def load_launcher(path):
    spec = importlib.util.spec_from_file_location("hermes_probe_launcher", path)
    if spec is None or spec.loader is None:
        raise ValueError("JEV-26 launcher must be a local Python file")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    return launcher


async def await_state(adapter, handle, context, previous, target):
    async with asyncio.timeout(15):
        while True:
            previous = await adapter.status(handle, context, previous=previous)
            if previous.state == target:
                return previous
            if previous.state in (AgentRunState.COMPLETED, AgentRunState.CANCELLED, AgentRunState.FAILED):
                raise RuntimeError(f"Expected {target} and observed {previous.state}")
            await asyncio.sleep(0.25)


async def exercise(origin, key, launcher, model):
    context = HermesExecutionContext("loopback-probe-principal")
    finished_key = secrets.token_hex(16)
    ongoing_key = secrets.token_hex(16)
    async with AsyncExitStack() as cleanup:
        adapter = HermesAgentAdapter(HermesHttpTransport(origin, key))
        cleanup.push_async_callback(adapter.close)
        receipt = await adapter.submit(
            HermesTask("read_only", {"query": launcher.SUCCESS}), context, correlation_id=finished_key,
        )
        assert receipt.state == AgentRunState.SUBMITTED
        finished = await await_state(adapter, receipt.handle, context, receipt, AgentRunState.COMPLETED)
        assert finished.result == "AVI_JEV26_OK"

        live = await adapter.submit(
            HermesTask("read_only", {"query": launcher.CANCEL}), context, correlation_id=ongoing_key,
        )
        entered = await asyncio.to_thread(model.entered[launcher.CANCEL].wait, 15)
        assert entered
        await adapter.close()

        fresh = HermesAgentAdapter(HermesHttpTransport(origin, key))
        cleanup.push_async_callback(fresh.close)
        recovered = await fresh.reconcile(context, correlation_id=finished_key, handle=receipt.handle)
        assert recovered == finished
        assert await fresh.reconcile(context, correlation_id=ongoing_key) is None
        active = await fresh.reconcile(context, correlation_id=ongoing_key, handle=live.handle)
        assert active is not None and active.state == AgentRunState.RUNNING
        assert await fresh.cancel(live.handle, context, previous=active) is None
        model.release[launcher.CANCEL].set()
        stopped = await await_state(fresh, live.handle, context, active, AgentRunState.CANCELLED)
        assert stopped.result is None

        denied = HermesAgentAdapter(HermesHttpTransport(origin, secrets.token_urlsafe(32)))
        cleanup.push_async_callback(denied.close)
        try:
            await denied.submit(
                HermesTask("read_only", {"query": launcher.SUCCESS}),
                context,
                correlation_id=secrets.token_hex(16),
            )
        except HermesError as error:
            assert error.code == HermesErrorCode.CREDENTIALS_REJECTED
            assert error.outcome == "denied"
        else:
            raise AssertionError("Hermes accepted an invalid bearer credential")
    assert model.calls[launcher.SUCCESS] == 1
    assert model.calls[launcher.CANCEL] == 1
    assert model.tool_counts == {launcher.SUCCESS: 0, launcher.CANCEL: 0}
    return {
        "hermes_revision": launcher.HERMES_COMMIT,
        "runtime": platform.python_version(),
        "model": "deterministic loopback",
        "submitted": str(receipt.state),
        "terminal": str(finished.state),
        "retained_result_matched": True,
        "active_after_detach": str(active.state),
        "cancel_acknowledgement": "no terminal evidence",
        "cancel_terminal": str(stopped.state),
        "wrong_credential": "credentials_rejected",
        "no_handle_recovery": "unresolved",
        "model_calls": sum(model.calls.values()),
        "model_tool_count": sum(model.tool_counts.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True, help="JEV-26 probe_hermes_runs.py")
    parser.add_argument("--checkout", type=Path, required=True, help="pinned Hermes source checkout")
    args = parser.parse_args()
    launcher = load_launcher(args.launcher.resolve())
    checkout = args.checkout.resolve()
    revision = subprocess.check_output(("git", "-C", str(checkout), "rev-parse", "HEAD"), text=True).strip()
    if revision != launcher.HERMES_COMMIT:
        raise ValueError("Hermes checkout must match the JEV-26 pin")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    key = secrets.token_urlsafe(32)
    with (
        tempfile.TemporaryDirectory(prefix="avi-hermes-adapter-") as state,
        launcher.model_server() as model,
        launcher.gateway(checkout, Path(state), model, port, key),
    ):
        evidence = asyncio.run(exercise(f"http://127.0.0.1:{port}", key, launcher, model))
    print(json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    main()
