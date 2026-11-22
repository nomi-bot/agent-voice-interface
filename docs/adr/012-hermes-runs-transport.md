# ADR-012: Hermes Runs transport

**Status** Proposed for Papi's review under [JEV-26](https://linear.app/jevenzzh/issue/JEV-26).

**Date** 2026-11-23

## Decision

Use the Hermes API Server's HTTP Runs endpoints on loopback with a canonical
session owner. Start with keyed submit, status and result polling, and the
explicit stop operation. Keep server authentication and validation inside the
Hermes adapter. Keep AVI task ownership, authorization, and retained event
replay in the AVI run service. [ADR-009](009-agent-run-lifecycle.md)

The API bearer grants control of its Hermes profile. Store it in the AVI
server and dedicate a Hermes profile to AVI. Limit the profile to the tools
required for the allowlisted task. The text-only probe uses an explicit
`platform_toolsets: {api_server: []}` in a fresh profile without MCP servers.
Authenticated `GET /v1/toolsets` reports enabled entries and the probe verifies
the actual model request carries zero tools. The catalog omits the default MCP
merge, so use the dedicated profile configuration as the tool boundary. A valid bearer cannot identify an AVI user
or establish their permission to approve or cancel a task. AVI checks those
permissions before using the bearer. The adapter leaves upstream approval to
an authorized human control path. [Hermes API Server security](https://github.com/NousResearch/hermes-agent/blob/0f602c5f6f18615632535251d62a2db74ce93b69/website/docs/user-guide/features/api-server.md#security)

The TUI Gateway can submit prompts, manage pending approvals, and resume a
session over JSON-RPC. Its shared WebSocket uses a single-use interactive
ticket and exposes session, configuration, and process controls. The Runs
endpoints cover the AVI task operations without depending on that local
interactive attachment or its larger method catalog. [Hermes TUI Gateway JSON-RPC](https://github.com/NousResearch/hermes-agent/blob/0f602c5f6f18615632535251d62a2db74ce93b69/website/docs/developer-guide/programmatic-integration.md#tui-gateway-json-rpc)

## Version and deployment

Pin [NousResearch/hermes-agent at 0f602c5](https://github.com/NousResearch/hermes-agent/tree/0f602c5f6f18615632535251d62a2db74ce93b69) from 2026-11-22.
The source checkout reports Hermes Agent `git.0f602c5 (2026.9.24)`. The
probe uses CPython 3.14.10 and a dedicated Hermes state directory. It runs
the gateway process and the real HTTP and SSE routes. A deterministic
loopback model returns a fixed text result. A separate temporary profile
enables the terminal tool to exercise a synthetic approval request.

Check authenticated `GET /v1/capabilities` before enabling this contract.
The probe requires `features.runs_idempotency.durable` and
`features.run_unknown_resolution` to be true. Hermes can fall back to
in-memory idempotency if it cannot open its store. In that case, AVI
cannot assume a retry after a restart will resolve the original run.
[Capabilities handler](https://github.com/NousResearch/hermes-agent/blob/0f602c5f6f18615632535251d62a2db74ce93b69/gateway/platforms/api_server.py#L2355-L2378),
[idempotency store](https://github.com/NousResearch/hermes-agent/blob/0f602c5f6f18615632535251d62a2db74ce93b69/gateway/platforms/api_server_run_idempotency.py#L62-L91)

## Observed contract

See the [redacted probe transcript](../roadmap/delivery/evidence/hermes-runs.json).

| Operation | Evidence | Contract |
| --- | --- | --- |
| Submit and duplicate | Verified | Send `POST /v1/runs` with a bearer, `Idempotency-Key`, and JSON `{"input": "<validated task text>"}`. Missing credentials return HTTP 401 and a missing input returns HTTP 400. HTTP 202 returns `run_id`, `status`, and `replayed`. A first receipt has status `started`. An identical retry returns the original handle, the current status, and `Idempotency-Replayed: true`. A changed body under the same key returns HTTP 409 with `error.code: idempotency_key_conflict`. |
| Status and result | Verified | Poll `GET /v1/runs/{run_id}`. Use `status` to classify the outcome and read `output` as the completed result. Treat the handle as opaque. |
| Stream and reconnect | Verified | `GET /v1/runs/{run_id}/events` emits SSE JSON with an `event` name, `run_id`, and numeric `seq`. `Last-Event-ID` resumes after that sequence while the buffer survives. Closing an active subscriber leaves its run active. The success marker arrives in `run.completed`. |
| Approval and identity | Verified | A manual approval parks the run at `waiting_for_approval`. A valid profile bearer and the current `request_id`, `execution_generation`, and `choice` resolve the approval. Missing and invalid credentials return HTTP 401. A stale generation returns HTTP 409. Denying the tool is visible to the worker, which can still complete the turn with a textual explanation. |
| Stop a running turn | Verified | `POST /v1/runs/{run_id}/stop` acknowledges a running turn as `stopping`. Poll until Hermes reports `cancelled` before marking the AVI run cancelled. |
| Recover a lost receipt | Verified | After an accepted turn entered the worker, killing the gateway and repeating its original submit under the same key recovered the handle. The run reported `unknown`, and the model saw one invocation. A completed run retained its result after restart. The old SSE stream returned HTTP 404. |
| Credential rotation | Verified | After restart with a rotated bearer, the prior bearer returned HTTP 401. The new bearer returned HTTP 404 for a handle created under the old bearer. |
| Standalone lookup by correlation key | Unsupported | The public Runs routes take a run handle. Recover a keyed receipt by replaying the original submit within the same deduplication scope. |
| Durable replay of upstream SSE | Unsupported | The pinned stream retains up to 1,000 recent entries in memory and reports `replay.truncated` on a gap. The nominal orphan stream TTL is 300 seconds. Use the AVI store for persistent replay. |
| Hosted scope grants, long-duration retention, and production model | Unverified | The probe uses one local profile with a synthetic model. The retention bound is declared by the runtime and sourced from the store constant. The probe does not wait for expiry or exercise hosted room grants or an external model. |

[Runs API reference](https://github.com/NousResearch/hermes-agent/blob/0f602c5f6f18615632535251d62a2db74ce93b69/website/docs/user-guide/features/api-server.md#runs-api-streaming-friendly-alternative),
[SSE replay implementation](https://github.com/NousResearch/hermes-agent/blob/0f602c5f6f18615632535251d62a2db74ce93b69/gateway/platforms/api_server_runs.py#L1290-L1345)

## Adapter and durability handoff

Generate a stable, opaque correlation key and persist it with the canonical
request body before dispatch. Hermes accepts 1–255 visible ASCII characters
and scopes a key by profile and bearer. The observed capability advertises
86,400 seconds of retention. Repeat the same request only through the same
profile and credential within the retained window. A change of scope or an
expired window requires reconciliation without a new dispatch. The status
`unknown` means the worker was interrupted without a settled outcome. Keep
the AVI intent uncertain and operator-reconcilable. Do not auto-resolve the
Hermes admission. [Run scope and replay](https://github.com/NousResearch/hermes-agent/blob/0f602c5f6f18615632535251d62a2db74ce93b69/gateway/platforms/api_server_runs.py#L420-L427),
[canonical status projection](https://github.com/NousResearch/hermes-agent/blob/0f602c5f6f18615632535251d62a2db74ce93b69/gateway/platforms/api_server_authority_runs.py#L66-L98)

Use bounded polling and cap response bodies before reading and parsing them.
Forward only verified progress with a validated shape and bounded data. Keep
raw tool arguments, previews, credentials, and upstream handles internal.
Map `failed`, `interrupted`, and `cancelled` separately from `unknown`.
Use the poll outcome after a stop acknowledgment or an SSE replay gap. An
approval resolution confirms the pending control choice. It does not make the
model's eventual result an authorization signal.
[Shared AVI handoffs](../roadmap/delivery/interfaces.md),
[canonical stop and approval](https://github.com/NousResearch/hermes-agent/blob/0f602c5f6f18615632535251d62a2db74ce93b69/gateway/platforms/api_server_authority_runs.py#L115-L163)

## Reproduce the probe

Run the following from the AVI checkout with Git and uv installed.

```sh
HERMES_CHECKOUT="$(mktemp -d)"
git -C "$HERMES_CHECKOUT" init -q
git -C "$HERMES_CHECKOUT" fetch --depth=1 --no-tags \
  https://github.com/NousResearch/hermes-agent.git 0f602c5f6f18615632535251d62a2db74ce93b69
git -C "$HERMES_CHECKOUT" checkout --detach FETCH_HEAD
uvx --from uv==0.13.9 uv sync --project "$HERMES_CHECKOUT" \
  --python 3.14.10 --locked --extra messaging --no-dev
"$HERMES_CHECKOUT/.venv/bin/hermes" --version
"$HERMES_CHECKOUT/.venv/bin/python" \
  docs/roadmap/delivery/evidence/probe_hermes_runs.py "$HERMES_CHECKOUT"
```

The probe starts Hermes on an ephemeral loopback port with temporary
`HERMES_HOME`, a random test bearer, and a fixed local model response.
It checks an admitted text-only task, a denied approval, and the settled
run retrieved after a restart. The trace contains synthetic text, aliases
for run and approval IDs, and selected wire fields. Event sequence numbers
and the status observed by an immediate retry can vary with scheduling.

## Review trigger

Repeat the probe before changing the pinned source or execution mode. JEV-27
repeats the approved subset against the configured Hermes deployment. JEV-28
verifies AVI restart, owner-bound authorization, and retry behavior around
the retained Hermes window.
