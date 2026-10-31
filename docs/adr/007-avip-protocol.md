# ADR-007: Versioned AVIP contract

**Status:** Accepted direction; wire schema proposed.

**Date:** 2026-10-30

## Context

The current `submit/status/ping` JSON socket has no audio framing, negotiated
capabilities or replay contract. Browser and Python clients need a common way to
correlate media, voice responses and agent events without inheriting one model SDK.

## Decision and lineage

Define a versioned, device-neutral AVIP contract with media, reliable control,
asynchronous events, negotiated capabilities and correlation IDs. It realizes the
client/transport boundary in [ADR-006](006-hexagonal-architecture.md) and preserves
[ADR-004](004-agent-independence.md)'s capability-gated agent behavior.
The [AVIP outline](../protocols/avip-v1.md) specifies the session handshake, its
`/ws/avip` wire rules, and the M0 binary media frame, including sequence,
timestamp, and bounded-delivery rules. Legacy `/ws` remains a separate protocol
until an explicit migration. `/ws/avip` implements the initial session handshake
only; it closes on binary frames and does not run the media queue. Reconnect
behavior, the remaining control messages, and compatibility with `/ws` are still
open.

## Alternatives and consequences

Extending browser-specific messages ad hoc avoids early schema work but risks
incompatible SDKs and provider-dependent semantics. A shared versioned contract
requires negotiation rules and common conformance fixtures for both clients.

## Review trigger

Revisit versioning or framing if M0 fixtures cannot express both clients' media and
control semantics, or a provider's ordering/backpressure constraints invalidate them.
