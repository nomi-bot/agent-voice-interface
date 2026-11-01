# Session, response and task lifecycles
Status: **State contracts implemented in `avi.core.lifecycle`.**
Reconnect is a legal session edge, but `/ws/avip` does not enter it.
Voice responses and agent runs are not created on that socket.
Durable run ownership remains gated on M2.

The three machines are synchronous and transport-independent. They share
correlation ids and nothing else. An illegal edge raises `InvalidTransition`
and leaves the machine unchanged: same state, revision, history, reason and
payload. Accepted edges append one history record and increment `revision`
by one. The same calls produce the same history. There is no clock.

Identifiers use the AVIP grammar `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. The
lifecycle module does not import the wire parser.

## VoiceSession

`connecting → active → reconnecting / closing → closed.`

| From | To | Operation |
| --- | --- | --- |
| connecting | active | `activate` |
| connecting | closing | `begin_close` |
| active | reconnecting | `lose_transport` |
| active | closing | `begin_close` |
| reconnecting | active | `reattach` |
| reconnecting | closing | `begin_close` |
| closing | closed | `finish_close` |

`closed` has no exit. `activate` requires a session id and is legal only from
`connecting`. `reattach` keeps that id. `lose_transport` keeps it too and does
not cancel a run.

`begin_close` freezes a `SessionCloseReason`: `ended`, `disconnected`,
`failed`, or `abandoned`. That reason is not a response or run outcome.
`cleanup(reason)` is `begin_close` plus `finish_close` from a live state, or
`finish_close` when already closing with the same reason. A second cleanup
with the same reason is a no-op once closed. A different reason is rejected
and does not relabel the session.

`/ws/avip` uses this machine for one socket:

- The session starts in `connecting`.
- `session.ready` is sent, then `activate` moves it to `active`.
- Peer disconnect and idle close 1001 use `cleanup(disconnected)`.
- Protocol and send closes 1002, 1003 and 1013 use `cleanup(failed)`.
- Any other local close code is also `failed`, so a new code is not recorded
  as a clean disconnect.
- The driver does not call `lose_transport` or `reattach`. There is still no
  reconnect handshake.
- The driver does not construct a `VoiceResponse` or an `AgentRun`.

## VoiceResponse

`requested → streaming → completed / interrupted / failed.`

| From | To | Operation |
| --- | --- | --- |
| requested | streaming | `start_streaming` |
| requested | interrupted | `interrupt` |
| requested | failed | `fail` |
| streaming | completed | `complete` |
| streaming | interrupted | `interrupt` |
| streaming | failed | `fail` |

`completed`, `interrupted` and `failed` have no exits. Completion is legal
only after streaming. Interrupt and failure are both legal before the first
audio, because cancel and provider failure can happen before a stream starts.

`interrupt` is provider-output cancellation. It leaves `failure_code` unset.
`fail` records a failure code and does not pass through `interrupted`.
Neither state can be rewritten into the other. A failure code is a lowercase
token such as `failed`, `timeout` or `provider_error`. It cannot name another
outcome (`cancelled`, `interrupted`, `completed`, and the non-terminal names).

`stop_playback` counts a client audio flush. It is legal in every state,
including a terminal one. It does not change state, revision or history, and
it does not interrupt generation or cancel a run.

`cleanup` is legal only from a terminal state. It marks the response released
and does not change that state. Calling it on `requested` or `streaming`
raises, so cleanup cannot choose interrupt versus failure. A second cleanup
is a no-op.

## AgentRun

`submitted → queued / running → completed / failed / cancelled.`

| From | To | Operation |
| --- | --- | --- |
| submitted | queued | `enqueue` |
| submitted | running | `start` |
| submitted | failed | `fail` |
| submitted | cancelled | `cancel` |
| queued | running | `start` |
| queued | failed | `fail` |
| queued | cancelled | `cancel` |
| running | completed | `complete` |
| running | failed | `fail` |
| running | cancelled | `cancel` |

`completed`, `failed` and `cancelled` have no exits. A direct `start` skips
the queue for an adapter that begins work immediately. `complete` is legal
only from `running`. An optional result string of at most 4096 characters is
payload, not a status.

`cancel` is an explicit owner request. It leaves `failure_code` unset.
`fail` records a failure code and does not pass through `cancelled`.
A cancelled run cannot fail, and a failed run cannot be cancelled.
Repeating `cancel` on an already cancelled run is rejected.

`cleanup` is legal only from a terminal state. A live run must be cancelled
or failed first. Cleanup does not pick one of those, and it does not relabel
the terminal state.

`session_id` on a run is correlation only. It may be omitted. Closing the
voice session that shares that id does not read or write the run.

## Separate operations

Correlate with `session_id`, `turn_id`, `response_id`, `run_id` and
`request_id` where the caller has them. Correlation does not couple transitions.

- `stop_playback` flushes client audio.
- `interrupt` asks a provider to stop the current response when that operation
  exists. The response outcome is `interrupted`.
- `cancel` asks an agent adapter to cancel a run when that adapter supports
  cancellation. The run outcome is `cancelled`.
- `fail` records a terminal failure of a response or a run. It is not cancel.

Disconnecting or closing a `VoiceSession` must not cancel an `AgentRun`.
That includes `lose_transport` and `cleanup`.

## Legacy mock and M2

The legacy `/ws` mock is unchanged. Its runs are session-owned and cancelled
on disconnect. That policy is not this state machine, and it is not durable
ownership. Do not route those mock runs through `AgentRun` until the
[M2 exit gates](../roadmap/milestones.md#m2-exit-gates) pass together.
[ADR-009](../adr/009-agent-run-lifecycle.md) records the decision.

The [AVIP draft](../protocols/avip-v1.md) still does not define the remaining
control and event messages (`session.end`, `playback.stop`, `response.cancel`,
`agent.run.cancel`, transcripts, and run status). Those messages are not how
this module is driven.
