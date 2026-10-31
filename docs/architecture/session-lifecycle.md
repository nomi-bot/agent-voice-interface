# Session, response and task lifecycles
Status: **Accepted conceptual separation; implementation pending.**

- VoiceSession: connecting → active → reconnecting / closing → closed.
- VoiceResponse: requested → streaming → completed / interrupted / failed.
- AgentRun: submitted → queued / running → completed / failed / cancelled.
- Correlate via session_id, turn_id, response_id, run_id, and request_id where appropriate.
- `stop_playback` flushes current client audio; `cancel_response` requests provider generation stop when supported; `cancel_task` explicitly requests agent cancellation when supported.
- Disconnecting voice must not automatically cancel a durable agent run. Runs require explicit owner, TTL, permission checks, persistence and reattachment policy before this behavior can be enabled.

**Existing skeleton exception:** current mock runs are session-owned and cancelled on disconnect for safety. The [M2 exit gates](../roadmap/milestones.md#m2-exit-gates) require authenticated
ownership independent of sessions, per-operation authorization, persistent run and
replay state, restart recovery, authorized reattachment and explicit legacy cutover.
Drain or explicitly cancel old session-owned runs and record their outcomes before
enabling durable submissions; do not silently promote them. All gates must pass
together. [ADR-009](../adr/009-agent-run-lifecycle.md) records the decision.

## Implemented AVIP connection

`/ws/avip` implements start → ready → closed for one WebSocket. The id is allocated
by the gateway, kept on that connection, and dropped on disconnect, idle timeout
or a fatal protocol close. A new socket sends `session.start` with `session_id`
null and receives a new id. Duplicate starts on the open socket are rejected.
This path has no AgentRun. The VoiceSession, VoiceResponse and AgentRun states
above, including reconnect, remain the target model. See the
[AVIP draft](../protocols/avip-v1.md) for envelopes and close codes.
