# JEV-27 · Pluggable Hermes runtime adapter

[Linear issue](https://linear.app/jevenzzh/issue/JEV-27). Begin with contract tests and a fake transport; bind real Hermes after JEV-26. Own src/avi/agents/hermes.py, new Hermes-only modules and focused tests. Coordinate any compatible extension of agents/base.py with JEV-28 before integration.

**Behavior.** The router supplies a validated, allowlisted task and authenticated execution context. Adapter maps it through the transport selected by JEV-26 and returns an opaque handle. Status translates the verified remote states into AVI lifecycle transitions. Poll only if verified and bound the interval, timeout and concurrent requests. Results are bounded to the existing lifecycle limits. Close subscription resources on detach without cancelling accepted tasks. Local shutdown must make outstanding submissions eligible for reconciliation.

| Condition | Output expected by durable service |
|---|---|
| Approval denied or credentials rejected | Typed denial, no alternative execution path |
| Missing progress or cancellation support | Capability false, explicit unsupported result |
| Accepted, then transport lost before response | Uncertain outcome plus available durable correlation, no blind resubmission |
| Cancellation acknowledged but still running | Preserve active state until terminal evidence arrives |
| Malformed upstream event or oversized output | Bounded, sanitized error; preserve last confirmed state for reconciliation |

**Tests and handoff.** Fake cases cover every row, duplicate or out-of-order terminal events, timeout and closing twice. Run the existing mock adapter contract tests unchanged. With the pinned real runtime demonstrate one read-only success, status/result, permission denial, and each capability claimed. Supply a redacted transcript and capability matrix keyed by version. Keep upstream URLs, credentials, vendor enums and SDK imports inside the adapter. Deliver the supported handle/reconciliation operations to JEV-28 and the structured error mapping to JEV-29.
