# ADR-009: Agent runs independent of voice sessions

**Status:** Accepted target; migration pending all M2 exit gates.

**Date:** 2026-10-30

## Context

A delegated Hermes task may take longer than a spoken turn or survive a dropped
voice connection. The current mock deliberately owns runs within one ControlSession
and cancels them on disconnect. Removing that cleanup alone would create orphan
work with no durable owner, authorization or recovery path.

## Decision and lineage

Separate VoiceSession, VoiceResponse and AgentRun. Playback stop, provider-response
cancellation and explicit agent-run cancellation remain distinct operations.
[ADR-004](004-agent-independence.md) stays accepted: Hermes details remain in its
adapter, with source-verified contracts and truthful status/cancel capabilities.
This ADR extends that boundary with run ownership and recovery; it does not change
[ADR-005](005-security-boundaries.md)'s local-only development restriction.

## M2 migration gate

The [canonical M2 exit gates](../roadmap/milestones.md#m2-exit-gates)
are required together before independent runs can be claimed:

- Persist an authenticated owner independent of session/device IDs, explicit TTL
  and lifecycle policy for each run.
- Check ownership/permissions on status, results, progress, cancel and reattach;
  reject cross-owner access and forged or expired credentials.
- Persist run identity, provider handle, state and replay metadata; demonstrate
  recovery after gateway restart without losing or duplicating a submission.
- Reattach an authorized new voice session and replay ordered events from a cursor,
  with documented retention, duplicate handling and expired-cursor behavior.
- Drain or explicitly cancel legacy session-owned in-flight work before cutover,
  recording outcomes; never silently relabel it as durable work.

The skeleton retains disconnect-cancels-run behavior until the complete migration
is implemented and tested. See also the [lifecycle guide](../architecture/session-lifecycle.md).

## Alternatives and consequences

Keeping every run session-owned simplifies cleanup but interrupts long tasks on a
voice disconnect. Independent runs require storage, authorization, event retention,
expiry and provider reconciliation. Uncertain provider outcomes must be recorded
and reconciled rather than retried blindly.

## Review trigger

Revisit if verified Hermes behavior cannot recover run handles, reconcile uncertain
submissions or meet ownership/cancellation guarantees. Narrow the supported behavior
and update M2 explicitly rather than claiming durability without evidence.
