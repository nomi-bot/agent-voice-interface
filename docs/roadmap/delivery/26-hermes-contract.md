# JEV-26 · Source-verified Hermes contract

[Linear issue](https://linear.app/jevenzzh/issue/JEV-26). Independent of #13. Papi owns the transport decision; another agent can prepare fake transport tests for JEV-27 now.

**Deliverables.** Commit an ADR with upstream repository, commit/tag, docs permalinks, runtime version, deployment mode and repeatable local startup/probe instructions. Compare HTTP/SSE API Server and TUI Gateway. Evaluate ACP only if the first two cannot cover the required operation. Use a bounded read-only sample task and a test identity. Archive redacted request/response and event fixtures.

| Probe | Evidence to capture |
|---|---|
| Submit twice with one correlation key | Acceptance signal, handle lifetime, whether deduplication or lookup is available |
| Status, progress and result | Message schema, ordering, terminal marker, polling fallback and replay availability |
| Approval and identity | Who can grant approval, denied behavior, scopes and behavior after revocation |
| Cancellation and disconnect | Acknowledgment versus terminal cancellation, whether task survives client exit |
| Crash or timeout after submit | Can an accepted request be found by correlation? Can a handle be recovered after restart? |

**Decision rule.** For each operation record verified, unsupported or unverified with the relevant probe. Choose the narrowest transport that preserves approvals and supports a real submit/result flow. Record gaps and their consequence for JEV-27/28. When a remote outcome cannot be queried, the run stays uncertain until reconciled. Unsupported progress must not become synthetic progress.

**Acceptance.** A second developer can reproduce success and rejection on the pinned version. Fixture fields match the observed exchange; no keys or private prompts are retained. The adapter owner can implement the supported subset without guessing endpoint names or cancellation semantics.
