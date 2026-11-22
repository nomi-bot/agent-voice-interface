# Hermes durable adapter

[JEV-27](../roadmap/delivery/27-hermes-adapter.md) defines the adapter contract. The transport in the contract tests is injected. Real transport enablement depends on the [JEV-26 probe evidence](../roadmap/delivery/26-hermes-contract.md).

## Run service handoff

The service verifies the principal and checks permissions before every operation. It persists the intent and a unique correlation ID before calling submit. It stores the opaque handle server-side and passes the last confirmed observation into later reads. The service owns idempotency and replay. The adapter issues one transport operation per call and drives no background polling.

| Example | Contract |
| --- | --- |
| Submit | `submit(HermesTask("read_only", {"query": "summarize the greeting"}), HermesExecutionContext("owner-a"), correlation_id="avi-request-1")` |
| Accepted | `HermesRun("remote-run-1", AgentRunState.RUNNING)` |
| Settled observation | `HermesRun("remote-run-1", AgentRunState.COMPLETED, result="greeting")` |
| Cancellation acknowledgement | `cancel(handle, context, previous=active)` can return `None`. The active observation remains current until terminal evidence arrives. |
| Recovery | `reconcile(context, correlation_id=key, handle=known_handle, previous=last_observation)` returns available evidence. A `None` response leaves the intent unresolved. |

`HermesError.code` is safe to classify and `HermesError.outcome` is `denied`, `unsupported`, `failed` or `uncertain`. A failed read leaves the previous observation available. An ambiguous submit returns `uncertain` plus the durable correlation ID. Closing the adapter releases local transport resources. Accepted work keeps its existing remote lifecycle.

## Capabilities and test scope

The fake transport exercises status, cancellation, progress and read-only lookup by correlation. Optional capabilities default to disabled and require verified support on the pinned runtime. A deployed transport with no read-only correlation lookup can reconcile a known handle by status. Unresolved submissions remain available for operator reconciliation.

`python -m unittest discover -s tests -p 'test_jev27_hermes_adapter.py' -v` runs the fake transport cases. Real acceptance still requires a pinned Hermes run, its status and result, a permission refusal, and a runtime demonstration of each advertised capability.
