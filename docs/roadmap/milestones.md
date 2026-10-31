# AVI v0.1 — approved M0–M4 roadmap
**Canonical implementation plan** for AVI v0.1; supersedes [the legacy roadmap](../roadmap.md).
Agreed planning baseline, 2026-10-30. Engineering estimates are provisional, not dates. **Linear project:** [P-JEV-1](https://linear.app/jevenzzh/project/agent-voice-interface-d0df264bb651).

## M0: Foundation & AVIP Protocol (4–5 engineer-days)
Deliver client/voice/agent ports, shared AVIP v1 proposal, transport abstraction, capability negotiation, session/run state machines and mocked conformance tests.
**Exit:** browser-shaped and headless Python mock clients connect to same gateway without special-case core logic.

**Implementation progress:** initial session-handshake validation and capability
negotiation have shared browser/headless fixtures. WebSocket integration, media
framing, state machines and runnable client conformance remain; M0 is not complete.

## M1: Real-Time Voice Streaming (5–7 days)
Verify official Gemini SDK/model and audio/session/tool contracts and pin a tested SDK range; implement Gemini Live provider, browser mic/speaker and binary audio transport, format/resampling/buffering, session/errors and measurement instrumentation.
**Exit:** browser can converse via AVI; record empirical latency, recovery and format evidence.

## M2: Hermes Agent Integration (4–6 days)
Validate actual Hermes contract; implement authenticated submit/status/results, run correlation, asynchronous progress and durable run lifecycle policy independent of voice-session disconnect.

### M2 exit gates

All gates below are required together. See [ADR-009](../adr/009-agent-run-lifecycle.md)
for the decision and [current skeleton behavior](../architecture.md) for the baseline.

1. **Verified integration:** record a source-verified Hermes submit/status/result,
   progress and cancellation contract and its permissions. A spoken command must
   trigger a real task and return its result to speech; unsupported capabilities
   must be reported truthfully.
2. **Ownership:** persist an authenticated owner independent of voice session and
   device IDs for every new durable run, plus creation/expiry times and an explicit
   TTL, retention and terminal-state cleanup policy. Disconnect and voice-response
   interruption must not cancel a properly owned run. Cancellation requires an
   authorized request or the documented expiry policy. Test TTL expiry and cleanup
   as well as normal completion and failure.
3. **Authorization:** check owner/permissions on status, result, progress subscription,
   cancellation and reattachment. Tests must cover cross-owner attempts, guessed
   run IDs, forged/expired credentials and revoked access. Reconnecting or knowing
   a run ID must never confer ownership. These checks do not permit remote exposure
   of the current development gateway under ADR-005.
4. **Persistence and recovery:** durably record owner, run ID, provider handle,
   submission correlation, state, expiry and event cursor/history. Demonstrate
   recovery after gateway restart during submission, execution and result delivery.
   Persist uncertain outcomes and reconcile them with the provider; duplicate
   submissions must not result from a blind retry. Document provider limitations
   and the recovery mechanism before claiming durability.
5. **Reattach and replay:** an authorized new voice session can attach to an existing
   run and receive retained progress/results in per-run order from a cursor. Define
   retention, duplicate detection and expired-cursor/snapshot behavior. Tests must
   cover disconnect during progress/result delivery, repeated reattachment and
   unavailable/expired cursors without losing the final result within retention.
6. **Legacy cutover:** stop accepting legacy session-owned submissions, drain them
   to terminal states or explicitly cancel them using the old policy, and record
   each outcome before enabling durable submissions. Unconfirmed remote outcomes
   block cutover until reconciled. Do not silently reassign old runs or promise
   replay for their unpersisted history. Test cutover with pending and running
   legacy work, and document the operational sequence.

**Exit evidence:** integration tests and a reproducible recovery/cutover report
must demonstrate every gate, including explicit cancel versus voice disconnect.
Until then, the mock remains session-owned and disconnect-cancels-run remains its
current behavior; implementing only part of these gates does not complete M2.

## M3: Full-Duplex & Robustness (5–8 days)
Barge-in, stop playback distinct from response/agent cancel, echo feedback, backpressure, timeout/reconnection, reliability tests.
**Exit:** interruption and network recovery tests pass without corrupting the running agent task.

## M4: Robot SDK, Simulator & Release (7–11 days)
Python SDK, headless robot simulator with mocked microphone/speaker/sensor/action, minimal optional ROS 2 bridge, protocol conformance, 30-minute stability run and reproducible deployment docs.
**Exit:** same core supports browser and simulated robot; ROS 2 two-way events and a **mock action** lifecycle pass. **No physical robot or actuator control.**

## Proposed (unverified) release targets
- Median simple-turn end-of-user-turn → first audible output ≤1.2 seconds.
- p95 user barge-in → playback stop ≤300 ms.
- ≥95% controlled Hermes delegation tests; full required AVIP conformance pass.
- 30-minute run without unrecoverable failure, documented load, CPU, memory and API costs.

Measure baselines first; revise targets based on tested environment, model/provider behavior and network topology. No fixed dates have been set.

## Scope exclusions
Physical robot integration, autonomous motion control, camera perception, WebRTC, mobile-native apps, local speech models and multi-tenant production deployment.

## Architecture references
[Overview](../architecture/overview.md) · [Protocol draft](../protocols/avip-v1.md) · [ADRs](../adr/)
