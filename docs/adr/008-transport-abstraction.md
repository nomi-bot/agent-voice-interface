# ADR-008: WebSocket-first transport abstraction

**Status:** Accepted target direction.

**Date:** 2026-10-30

## Context

The skeleton already uses WebSocket control, but binary audio and headless clients
are still planned. Building WebRTC, NAT traversal and media routing now would add
infrastructure before AVI has a measured browser-to-provider audio baseline.

## Decision and lineage

Use a transport port and implement WebSocket first. [ADR-002](002-transport-topology.md)
remains accepted: Browser → AVI Gateway → Gemini Live is the planned MVP topology,
with provider keys server-side. The new port generalizes client/transport mechanics;
it does not select a direct-to-provider path or move credentials into clients.

[ADR-003](003-livekit-decision.md) also remains accepted: neither LiveKit nor Pipecat
is a mandatory v0.1 dependency. WebRTC and gRPC are possible later adapters, not
v0.1 commitments. Adding a transport must preserve [AVIP](007-avip-protocol.md)
semantics and requires a follow-up decision supported by measurements.

## Alternatives and consequences

Immediate multi-transport support adds operational and compatibility work before a
baseline exists. A direct-only provider client changes the accepted topology.
WebSocket first keeps the dependency set small but leaves media buffering and
backpressure to AVI; the transport port must not promise identical wire framing.

## Review trigger

Revisit when measured latency, NAT traversal, multi-device media routing or scaling
requirements justify WebRTC/LiveKit/Pipecat or another transport, as ADR-003 anticipates.
