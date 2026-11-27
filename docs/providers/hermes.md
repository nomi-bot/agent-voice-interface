# Hermes durable adapter

[JEV-27](../roadmap/delivery/27-hermes-adapter.md) defines the adapter contract. The injected transport supports contract tests. The HTTP transport consumes [JEV-26's pinned runtime evidence](../roadmap/delivery/evidence/hermes-runs.json).

## Run service handoff

The service verifies the principal and checks permissions before every operation. It persists the intent and a unique correlation ID before calling submit. It stores the opaque handle server-side and passes the last confirmed observation into later reads. The service owns idempotency and replay. The adapter issues one transport operation per call and drives no background polling.

| Example | Contract |
| --- | --- |
| Submit | `submit(HermesTask("read_only", {"query": "summarize the greeting"}), HermesExecutionContext("owner-a"), correlation_id="avi-request-1")` |
| Accepted | `HermesRun("remote-run-1", AgentRunState.SUBMITTED)` persists the handle before the first status read. |
| Settled observation | `HermesRun("remote-run-1", AgentRunState.COMPLETED, result="greeting")` |
| Cancellation acknowledgement | `cancel(handle, context, previous=active)` can return `None`. The active observation remains current until terminal evidence arrives. |
| Recovery | `reconcile(context, correlation_id=key, handle=known_handle, previous=last_observation)` returns available evidence. A `None` response leaves the intent unresolved. |

`HermesError.code` is safe to classify and `HermesError.outcome` is `denied`, `unsupported`, `failed` or `uncertain`. A failed read leaves the previous observation available. An ambiguous submit returns `uncertain` plus the durable correlation ID. Closing the adapter releases local transport resources. Accepted work keeps its existing remote lifecycle.

## Pinned loopback HTTP transport

Install the optional dependency with `python -m pip install '.[hermes]'`. Construct `HermesAgentAdapter(HermesHttpTransport(origin, api_key))` with a numeric loopback origin and a dedicated server bearer credential. Use Hermes commit `0f602c5f6f18615632535251d62a2db74ce93b69` and an isolated profile with `platform_toolsets.api_server` set to an empty list. The [JEV-26 launcher](../roadmap/delivery/evidence/probe_hermes_runs.py) provides the profile configuration used in the probe. Keep the bearer in server configuration and the profile's execution environment isolated.

The HTTP adapter accepts the `read_only` task kind and one nonblank `query` string of up to 4096 characters. It sends the persisted correlation ID as the idempotency key. Generate a fresh correlation ID for each durable admission across principals sharing a Hermes profile. An accepted HTTP receipt supplies the handle. A subsequent status read confirms execution and returns the bounded result. A pending human approval remains an active run. Direct credential or permission refusals become typed denials.

| Capability | Pinned HTTP behavior |
| --- | --- |
| Status and result | Enabled. A fresh adapter can read a retained result by handle. |
| Cancellation | Enabled. A stop acknowledgement leaves the prior active state current. A terminal status confirms cancellation. |
| Progress | Disabled. |
| Read-only correlation lookup | Disabled. Missing or unknown evidence remains unresolved. |

## Verification

`python -m unittest discover -s tests -p 'test_jev27_hermes*.py' -v` exercises the injected transport and HTTP wire mapping. The [redacted loopback result](../roadmap/delivery/evidence/hermes-adapter-loopback.json) records the actual pinned Hermes daemon with CPython 3.14.10 and a deterministic local model. The test reads a completed result after reattachment, observes an active run after detach, cancels it explicitly, and checks credential denial. Both model calls received zero tools.

With the JEV-26 dependencies installed and a checkout of the pinned Hermes source, run the probe using the launcher from JEV-26 or the integrated dev branch.

```sh
PYTHONPATH=src python docs/roadmap/delivery/evidence/probe_hermes_adapter.py \
  --launcher /path/to/probe_hermes_runs.py \
  --checkout /path/to/pinned/hermes-agent
```

The [local gateway quickstart](../../README.md#run-the-local-hermes-gateway)
runs the AVIP routes, authorization, SQLite store, voice journal and maintenance
loop as one process. Hermes accepts an API key in its dedicated server profile.
AVI reads that key from `HERMES_API_KEY`. Only the short-lived AVI client grant
goes into the browser. Reconnection requires a fresh handshake and authentication.

The [integrated AVIP probe](../roadmap/delivery/evidence/probe_hermes_avip.py)
uses a pinned local Hermes checkout and a deterministic model to exercise the
gateway without a microphone. See the [release checks](../roadmap/delivery/release.md)
for the separate live-provider and device evidence.
