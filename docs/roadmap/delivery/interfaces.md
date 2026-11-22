# Shared handoffs for parallel development

This is a proposed internal integration contract. Existing Python ports, AVIP envelopes, binary framing, and legacy callers remain the baseline. The names below describe fields and operations for new fixtures. Publish the agreed typed definitions and golden request/response examples with their owning PR before live wiring. Do not copy independently evolving interfaces into each adapter.

| Boundary | Producer → consumer | Payload and responsibility |
|---|---|---|
| Provider event | JEV-32/29 → tool router | Existing `VoiceEvent` and `VoiceProvider.tool_result(call_id, result)`. Adapter translates vendor call names/args to a validated allowlisted operation. Use the provider call ID for the reply. |
| Authorization | JEV-28 → run service / router | Verified principal and permissions supplied by trusted session setup. Session ID, model text, tool arguments and device labels cannot assert ownership. Check live permission at every read, cancel or attach. |
| Task dispatch | JEV-29 → JEV-28 → JEV-27 | Principal, stable request ID, allowlisted task kind and validated arguments. Reserve once per (owner, request ID). Same payload returns same run, conflicting payload returns conflict. Persist correlation before transport send. |
| Provider handle | JEV-27 → JEV-28 | Return opaque upstream handle and verified capabilities, or a typed denied/unsupported/failed/uncertain outcome. Preserve uncertainty across retries. Keep upstream handles server-side. |
| Event replay | JEV-28 → JEV-29/30/31 | Owner-authorized read by AVI run ID and exclusive `after_cursor`; ordered events contain run ID, per-run cursor, kind and bounded payload. A snapshot includes terminal state/result if retained and a resume cursor. |
| Output generation | JEV-32 → JEV-30/31 | Use #13's `response.interrupted` generation semantics once merged. A delayed interruption for generation N never flushes N+1. Audio remains AVIP v1 PCM. |
| Simulator events | JEV-31 → optional bridge | Device-side mock event ID, kind, capability and bounded payload. Local authorization precedes mock action. Bridge owns mapping to ROS messages; core has no ROS dependency. |

**Shared example.** Owner A submits request Q, receives AVI run R and event cursor 1. Hermes completion yields cursor 2 or later. A second delivery of Q binds to R; a second subscriber may receive the same cursor and deduplicates by (R, cursor). Owner B receives the same denial for R as for an unknown ID. A new voice session for A may fetch a retained result after authentication. Only the authenticated run service can resolve R to an upstream handle.

**Wire evolution.** JEV-28 owns the proposed durable run operation schemas and Python/JS conformance vectors; JEV-29 registers negotiated gateway operations and JEV-30 consumes them. Define names, direction, capability/version negotiation, sizes, error codes and close behavior in the protocol draft before using a new envelope. Include one positive, unsupported capability, malformed frame and legacy-session vector. Distinguish run reattachment to a fresh socket from provider voice-session resumption. Media-only clients keep the existing handshake and binary framing.
