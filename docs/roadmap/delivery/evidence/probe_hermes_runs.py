"""Exercise the pinned Hermes gateway against a deterministic loopback model."""

import argparse
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx


HERMES_COMMIT = "0f602c5f6f18615632535251d62a2db74ce93b69"
MODEL = "avi-hermes-probe"
SUCCESS = "Return the fixed marker AVI_JEV26_OK."
APPROVAL = "Exercise the approval denial fixture."
CANCEL = "Exercise the stop fixture."
CRASH = "Exercise the lost receipt fixture."
RUNS = "/v1/runs"
HTTP_TIMEOUT = 10
SETTLE_TIMEOUT = 30


class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = request["messages"]
        user_text = next((m.get("content") for m in reversed(messages) if m["role"] == "user"), "")
        if user_text not in {SUCCESS, APPROVAL, CANCEL, CRASH}:
            self.send_error(400)
            return
        with self.server.lock:
            self.server.calls[user_text] += 1
            self.server.tool_counts[user_text] = len(request.get("tools") or [])
        if user_text in self.server.release:
            self.server.entered[user_text].set()
            self.server.release[user_text].wait(SETTLE_TIMEOUT * 2)
        message = {"role": "assistant", "content": "AVI_JEV26_OK"}
        finish = "stop"
        if user_text == APPROVAL:
            if messages[-1]["role"] == "tool":
                if "BLOCKED" in str(messages[-1]["content"]):
                    self.server.denial_seen.set()
                message["content"] = "AVI_JEV26_DENIAL_OBSERVED"
            else:
                message.update(content=None, tool_calls=[{
                    "id": "call_avi_approval",
                    "type": "function",
                    # No operands. This reaches the approval gate without
                    # naming a file or directory that could be removed.
                    "function": {"name": "terminal", "arguments": '{"command":"rm -r","timeout":1}'},
                }])
                finish = "tool_calls"
        streaming = request.get("stream", False)
        choice = {"index": 0, "delta" if streaming else "message": message, "finish_reason": finish}
        if streaming and "tool_calls" in message:
            message["tool_calls"][0]["index"] = 0
        payload = json.dumps({
            "id": "chatcmpl-avi-probe",
            "object": "chat.completion.chunk" if streaming else "chat.completion",
            "model": MODEL,
            "choices": [choice],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        if streaming:
            payload = b"data: " + payload + b"\n\ndata: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


@contextmanager
def model_server():
    with ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler) as server:
        server.lock = threading.Lock()
        server.calls = Counter()
        server.tool_counts = {}
        server.denial_seen = threading.Event()
        server.entered = {text: threading.Event() for text in (CANCEL, CRASH)}
        server.release = {text: threading.Event() for text in server.entered}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            for event in server.release.values():
                event.set()
            server.shutdown()
            thread.join(timeout=HTTP_TIMEOUT)


def wait_for(operation, description, process=None):
    deadline = time.monotonic() + SETTLE_TIMEOUT
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"Hermes exited with status {process.returncode} during {description}")
        result = operation()
        if result:
            return result
        time.sleep(0.05)
    raise TimeoutError(description)


@contextmanager
def gateway(checkout, state, model, port, key, *, toolsets=()):
    model_url = f"http://127.0.0.1:{model.server_port}/v1"
    state.mkdir(exist_ok=True, mode=0o700)
    (state / "config.yaml").write_text(json.dumps({
        "_config_version": 12,
        "gateway": {
            "platforms": {"api_server": {"enabled": True, "host": "127.0.0.1", "port": port}},
            "multiplex_profiles": False,
        },
        "model": {"provider": "custom", "default": MODEL, "base_url": model_url, "context_length": 256_000},
        "auxiliary": {"title_generation": {"enabled": False}},
        "platform_toolsets": {"api_server": list(toolsets)},
        "terminal": {"cwd": str(state)},
        "approvals": {"mode": "manual"},
    }))
    (state / "models_dev_cache.json").write_text(json.dumps({
        MODEL: {"id": MODEL, "env": [], "models": {}},
    }))
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(checkout),
        "XDG_STATE_HOME": str(state / "xdg-state"),
        "HERMES_HOME": str(state),
        "HERMES_DISABLE_LAZY_INSTALLS": "1",
        "OPENAI_API_KEY": "fixture-model-loopback-only",
        "OPENAI_BASE_URL": model_url,
        "API_SERVER_KEY": key,
        "API_SERVER_ENABLED": "true",
        "API_SERVER_PORT": str(port),
        "PYTHONUNBUFFERED": "1",
    }
    with (state / "gateway.log").open("w+") as log, httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {key}"},
        timeout=HTTP_TIMEOUT,
        trust_env=False,
    ) as client:
        process = subprocess.Popen(
            [sys.executable, "-m", "gateway.run"],
            cwd=state,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            def ready():
                try:
                    response = client.get("/v1/capabilities")
                except httpx.TransportError:
                    return None
                return response.json() if response.status_code == 200 else None

            capabilities = wait_for(ready, "API Server readiness", process)
            yield process, client, capabilities
        except Exception:
            log.flush()
            log.seek(0)
            # State is generated for this probe. Redact both test credentials
            # even on a failing run, and cap the diagnostic size.
            print(log.read().replace(key, "[redacted]").replace(
                env["OPENAI_API_KEY"], "[redacted]"
            )[-4_096:], file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=HTTP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=HTTP_TIMEOUT)


def body(response, expected):
    if response.status_code != expected:
        raise RuntimeError(f"{response.request.method} returned HTTP {response.status_code}, expected {expected}")
    return response.json()


def submit(client, text, correlation):
    return client.post(RUNS, json={"input": text}, headers={"Idempotency-Key": correlation})


def poll(client, run_id, *statuses):
    def query():
        status = body(client.get(f"{RUNS}/{run_id}"), 200)
        if status["status"] in statuses:
            return status
        if status["status"] in {"completed", "cancelled", "failed", "interrupted", "unknown"}:
            raise RuntimeError(f"Hermes returned {status['status']} while waiting for {statuses}")
        return None

    return wait_for(query, f"run status in {statuses}")


def events(stream):
    sequence = None
    for line in stream.iter_lines():
        if line.startswith("id:"):
            sequence = int(line[3:].strip())
        if line.startswith("data:"):
            event = json.loads(line[5:])
            assert event.get("seq") == sequence
            yield event
            sequence = None


def run_receipt(response, alias):
    receipt = response.json()
    return {
        "http_status": response.status_code,
        "body": {"run_id": alias, "status": receipt["status"], "replayed": receipt["replayed"]},
        "idempotency_replayed": response.headers.get("Idempotency-Replayed"),
    }


def result_fields(result, alias):
    return {key: alias if key == "run_id" else result[key]
            for key in ("object", "run_id", "status", "output") if key in result}


def read_only(client, model):
    toolsets = body(client.get("/v1/toolsets"), 200)
    assert toolsets["object"] == "list" and toolsets["platform"] == "api_server"
    enabled = [entry["name"] for entry in toolsets["data"] if entry["enabled"]]
    assert not enabled
    anonymous = client.post(RUNS, json={"input": SUCCESS}, headers={"Authorization": ""})
    assert anonymous.status_code == 401
    malformed = client.post(RUNS, json={})
    malformed_error = body(malformed, 400)["error"]
    accepted = submit(client, SUCCESS, "avi-success")
    run_id = body(accepted, 202)["run_id"]
    replay = submit(client, SUCCESS, "avi-success")
    assert body(replay, 202)["run_id"] == run_id
    assert replay.headers["Idempotency-Replayed"] == "true"
    conflict = submit(client, APPROVAL, "avi-success")
    assert body(conflict, 409)["error"]["code"] == "idempotency_key_conflict"
    status = poll(client, run_id, "completed")
    assert status["output"] == "AVI_JEV26_OK"
    assert model.calls[SUCCESS] == 1 and model.tool_counts[SUCCESS] == 0
    with client.stream("GET", f"{RUNS}/{run_id}/events") as stream:
        assert stream.status_code == 200
        terminal = [event for event in events(stream) if event["event"] == "run.completed"]
    assert len(terminal) == 1
    return run_id, {
        "toolsets": {"http_status": 200, "enabled": enabled, "model_tools": model.tool_counts[SUCCESS]},
        "request": {"method": "POST", "path": RUNS,
                    "idempotency_key": "avi-success", "body": {"input": SUCCESS}},
        "submit": run_receipt(accepted, "run_redacted_success"),
        "missing_auth_http_status": anonymous.status_code,
        "malformed": {"http_status": malformed.status_code, "type": malformed_error["type"],
                      "code": malformed_error.get("code")},
        "duplicate": run_receipt(replay, "run_redacted_success"),
        "conflict": {"http_status": conflict.status_code, "code": "idempotency_key_conflict"},
        "status": result_fields(status, "run_redacted_success"),
        "event": {"event": terminal[0]["event"], "seq": terminal[0]["seq"],
                  "run_id": "run_redacted_success", "output": terminal[0]["output"]},
        "model_requests": model.calls[SUCCESS],
    }


def approval_denial(client, model):
    run_id = body(submit(client, APPROVAL, "avi-denial"), 202)["run_id"]
    path = f"{RUNS}/{run_id}"
    with client.stream("GET", path + "/events") as stream:
        assert stream.status_code == 200
        prompt_event = next(event for event in events(stream) if event["event"] == "approval.request")
    pending = poll(client, run_id, "waiting_for_approval")["pending_controls"][0]
    assert pending["prompt_id"] == prompt_event["prompt_id"]
    decision = {"request_id": pending["prompt_id"],
                "execution_generation": pending["execution_generation"], "choice": "deny"}
    missing_auth = client.post(path + "/approval", json=decision, headers={"Authorization": ""})
    wrong_auth = client.post(path + "/approval", json=decision, headers={"Authorization": "Bearer invalid"})
    stale = client.post(path + "/approval", json={**decision, "execution_generation": decision["execution_generation"] + 1})
    assert missing_auth.status_code == wrong_auth.status_code == 401
    assert stale.status_code == 409
    with client.stream("GET", path + "/events", headers={"Last-Event-ID": str(prompt_event["seq"])}) as stream:
        assert stream.status_code == 200
        decision_receipt = body(client.post(path + "/approval", json=decision), 200)
        assert decision_receipt["status"] == "resolved"
        continued = list(events(stream))
    assert continued
    assert min(event["seq"] for event in continued) > prompt_event["seq"]
    status = poll(client, run_id, "completed")
    assert model.denial_seen.is_set() and status["output"] == "AVI_JEV26_DENIAL_OBSERVED"
    return {
        "pending_control": {"kind": pending["kind"], "prompt_id": "prompt_redacted",
                            "execution_generation": pending["execution_generation"]},
        "approval_event": {"event": prompt_event["event"], "seq": prompt_event["seq"]},
        "approval_request": {**decision, "request_id": "prompt_redacted"},
        "missing_auth_http_status": missing_auth.status_code,
        "wrong_auth_http_status": wrong_auth.status_code,
        "stale_approval_http_status": stale.status_code,
        "decision_receipt": {"http_status": 200, "status": decision_receipt["status"]},
        "reconnected_event_sequence": [{"event": event["event"], "seq": event["seq"]} for event in continued],
        "tool_denied": model.denial_seen.is_set(),
        "status": result_fields(status, "run_redacted_denial"),
    }


def stop_run(client, model):
    run_id = body(submit(client, CANCEL, "avi-stop"), 202)["run_id"]
    assert model.entered[CANCEL].wait(SETTLE_TIMEOUT)
    response = client.post(f"{RUNS}/{run_id}/stop", json={})
    acknowledgment = body(response, 200)
    assert acknowledgment["status"] == "stopping"
    model.release[CANCEL].set()
    terminal = poll(client, run_id, "cancelled")
    return {
        "stop": {"http_status": response.status_code, "status": acknowledgment["status"]},
        "status": result_fields(terminal, "run_redacted_stop"),
    }


def probe(checkout, state):
    key = secrets.token_urlsafe(32)
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    with model_server() as model:
        with gateway(checkout, state, model, port, key) as (process, client, capabilities):
            feature = capabilities["features"]["runs_idempotency"]
            assert feature["supported"] and feature["durable"]
            assert capabilities["features"]["run_unknown_resolution"]
            success_id, success = read_only(client, model)
            cancellation = stop_run(client, model)
            # Discard the accepted handle as a real client could after losing
            # its response. Wait for worker entry before killing the probe's
            # own gateway process.
            assert submit(client, CRASH, "avi-lost-receipt").status_code == 202
            assert model.entered[CRASH].wait(SETTLE_TIMEOUT)
            process.kill()
            process.wait(timeout=HTTP_TIMEOUT)
        model.release[CRASH].set()
        with gateway(checkout, state, model, port, key) as (_, client, _):
            replayed = submit(client, CRASH, "avi-lost-receipt")
            crash_id = body(replayed, 202)["run_id"]
            assert replayed.headers["Idempotency-Replayed"] == "true"
            unresolved = poll(client, crash_id, "unknown")
            assert model.calls[CRASH] == 1
            success_replayed = submit(client, SUCCESS, "avi-success")
            assert body(success_replayed, 202)["run_id"] == success_id
            assert success_replayed.headers["Idempotency-Replayed"] == "true"
            assert poll(client, success_id, "completed")["output"] == "AVI_JEV26_OK"
            assert model.calls[SUCCESS] == 1
            lost_stream = client.get(f"{RUNS}/{success_id}/events")
            assert lost_stream.status_code == 404
        with gateway(checkout, state, model, port, secrets.token_urlsafe(32)) as (_, client, _):
            revoked = client.get(f"{RUNS}/{success_id}", headers={"Authorization": f"Bearer {key}"})
            new_key_old_run = client.get(f"{RUNS}/{success_id}")
            assert revoked.status_code == 401 and new_key_old_run.status_code == 404
        with gateway(checkout, state / "approval", model, port, secrets.token_urlsafe(32),
                     toolsets=("terminal",)) as (_, client, _):
            approval = approval_denial(client, model)
        return {
            "upstream_commit": HERMES_COMMIT,
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
            "mode": "Hermes gateway API Server with a canonical local owner and loopback model fixture",
            "idempotency_capability": feature,
            "read_only": success,
            "approval_denial": approval,
            "stop": cancellation,
            "restart": {
                "lost_receipt_replay": run_receipt(replayed, "run_redacted_unknown"),
                "after_crash": result_fields(unresolved, "run_redacted_unknown"),
                "crashed_turn_model_requests": model.calls[CRASH],
                "completed_receipt_replay": run_receipt(success_replayed, "run_redacted_success"),
                "completed_model_requests": model.calls[SUCCESS],
                "events_after_restart_http_status": lost_stream.status_code,
                "old_key_http_status": revoked.status_code,
                "new_key_old_run_http_status": new_key_old_run.status_code,
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path, help="Pinned NousResearch/hermes-agent checkout")
    args = parser.parse_args()
    checkout = args.checkout.resolve()
    actual_commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if actual_commit != HERMES_COMMIT:
        parser.error(f"Hermes checkout must be at {HERMES_COMMIT}")
    with tempfile.TemporaryDirectory(prefix="avi-hermes-probe-") as folder:
        print(json.dumps(probe(checkout, Path(folder).resolve()), indent=2))


if __name__ == "__main__":
    main()
