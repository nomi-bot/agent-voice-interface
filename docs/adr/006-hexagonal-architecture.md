# ADR-006: Hexagonal architecture

**Status:** Accepted target direction; implementation incremental.

**Date:** 2026-10-30

## Context

AVI's initial FastAPI control socket coordinates a mock agent, while browser audio,
Gemini, Hermes and a headless robot client are still pending. Adding these directly
to request handlers would couple provider APIs and device behavior to session logic
before a second client can validate portability.

## Decision and lineage

Use ports and adapters for clients, transports, voice providers, agent runtimes and
device actions. Keep gateway coordination independent of concrete integrations.
This extends [ADR-001](001-python-control-plane.md): Python 3.12+, asyncio and
FastAPI remain the control plane; no language rewrite is selected. It also preserves
[ADR-002](002-transport-topology.md): clients connect through the gateway and
provider keys stay server-side. [ADR-004](004-agent-independence.md) continues to
place Hermes-specific behavior inside its adapter.

## Alternatives and consequences

A monolithic web app would reduce initial interfaces but bind the browser to
providers. Direct model SDKs in clients would duplicate orchestration and change
the accepted credential boundary. Ports add interface and conformance-test cost;
M0 must show browser-shaped and Python clients using the same core without
device-type branches. Module names remain provisional.

## Review trigger

Revisit port boundaries if M0 conformance needs device-specific core logic or real
provider integration shows that a port cannot express required behavior.
