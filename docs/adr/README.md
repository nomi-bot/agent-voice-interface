# Architecture decision records

ADRs use one monotonically increasing, three-digit sequence: `001`, `002`, ….
Never reuse an identifier or renumber an established decision. New records in
this PR are 006–010; the draft 0001–0005 names were removed to avoid collisions.

## Decision map

- [001: Python control plane](001-python-control-plane.md) remains accepted;
  [006: Hexagonal architecture](006-hexagonal-architecture.md) extends its boundaries.
- [002: Gateway topology](002-transport-topology.md) remains accepted for the MVP;
  [008: Transport abstraction](008-transport-abstraction.md) preserves that topology.
- [003: LiveKit and Pipecat](003-livekit-decision.md) remains accepted;
  [008](008-transport-abstraction.md) preserves their optional, deferred status.
- [004: Agent independence](004-agent-independence.md) remains accepted;
  [009: Agent run lifecycle](009-agent-run-lifecycle.md) adds the target ownership
  and recovery model without claiming it is implemented.
- [005: Local security boundary](005-security-boundaries.md) remains accepted for
  development; future run authorization in [009](009-agent-run-lifecycle.md)
  does not authorize exposing the current gateway remotely.
- [006: Hexagonal architecture](006-hexagonal-architecture.md) defines target ports
  and adapters while retaining Python/FastAPI and the gateway topology.
- [007: AVIP protocol](007-avip-protocol.md) accepts a versioned contract direction;
  the wire schema is still proposed.
- [008: Transport abstraction](008-transport-abstraction.md) selects WebSocket first.
- [009: Agent run lifecycle](009-agent-run-lifecycle.md) separates voice and runs;
  durable behavior is gated on M2.
- [010: Robot client integration](010-robot-client-integration.md) selects a Python
  simulator and optional robot-side ROS 2 bridge; physical actuation is excluded.

## Review policy

Accepted direction is not evidence of implementation. Each ADR records its own
context, alternatives, consequences and review trigger. When evidence requires a
change, add a new ADR and cross-link both records with an explicit supersession
status; retain the historical record. Validate provisional details against the
[canonical milestone gates](../roadmap/milestones.md) before claiming delivery.
