# AVIP durable runs extension proposal

This [JEV-28](https://linear.app/jevenzzh/issue/JEV-28) draft defines handoff
fixtures for the opt-in [AVIP v1](avip-v1.md) control extension.
Servers with a configured service negotiate these operations with compatible clients. The
[fixtures](../../tests/fixtures/jev28-durable-runs-v1.json) include synthetic
positive, denied, unsupported, malformed and legacy traces.

## Connection and authorization

Offer the boolean `durable_runs` capability in the regular `session.start`.
The extension is active only when both peers enable it in `session.ready`.
This boolean under `avip/1` selects version 1 of the extension. A future
incompatible extension needs a separately negotiated capability. Where
unsupported, the gateway omits the capability and returns the existing
`unsupported_message_type` error for its controls. A legacy media client uses
the existing handshake and binary framing.

After negotiation, send an opaque bearer grant in a single loopback
`agent.run.authenticate` text frame. Trusted local server code issues grants.
Pass them in the authentication frame and discard credentials before logging.
The handler stores the verified connection grant server-side and replies with
`agent.run.authenticated`. A reconnect requires a fresh handshake and
authentication. Owner, provider handle and provider scope stay in the server.
A previously authenticated socket cannot switch principal in place.

Check grants, expiry, revocation and persisted ownership on each operation.
`attach` requires `attach`, `progress` and `result` grants since replay can
contain both payloads. A `progress` request requires only `progress` and its
replay omits all result text. An unknown or cross-owner run, invalid grant
and missing permission all produce `access_denied` with a null field.
These operation errors leave the session open for media.

## Control messages

Each message is one AVIP text frame with `protocol: "avip/1"` and `type`. This
table lists the remaining required fields. One operation at a time per socket
keeps existing AVIP error correlation by request order, while audio can
continue. The worker buffers at most one waiting operation. Exceeding
that bound closes the socket with 1013. Replay sends frames in cursor order followed by a receipt.
A client advances a cursor only after consuming the associated event or
snapshot. Cursors are exclusive, nonnegative JSON safe integers.

| Client to gateway | Required fields | Gateway to client |
| --- | --- | --- |
| `agent.run.authenticate` | `token` (43-character base64url grant) | `agent.run.authenticated` |
| `agent.run.submit` | `request_id`, `kind`, `arguments` | `agent.run.status` |
| `agent.run.status` | `run_id` | `agent.run.status` |
| `agent.run.result` | `run_id` | `agent.run.result` |
| `agent.run.attach` | `run_id`, `after_cursor` | Zero or more `agent.run.event` frames or one `agent.run.snapshot`, then `agent.run.replay` |
| `agent.run.progress` | `run_id`, `after_cursor` | Same replay sequence with no result text |
| `agent.run.cancel` | `run_id` | `agent.run.status` |

`agent.run.status` carries `run_id`, `state`, `dispatch`, `cursor`,
`expires_at` (Unix seconds), `expired`, `failure_code` (or null), and
`reconciliation_pending`. Result and snapshot responses carry that object
under `status`, boolean `retained`, and `result` (string or null).
`agent.run.event` carries `run_id`, `cursor`, `kind` and `payload`.
An event is addressed by the pair `(run_id, cursor)`. `agent.run.replay`
carries `run_id` and `resume_cursor`. A snapshot and its resume cursor come
from the same database read transaction.

`request_id`, `run_id` and `kind` use AVIP identifiers. The router validates
the allowlisted kind and its JSON arguments before submission. A server-side
digest binds kind and arguments to the owner's request ID. Repeating the same
task returns its run. A different task with that key returns
`request_conflict`. The canonical task is at most 4096 ASCII bytes.
Result and progress text are each at most 4096 code points and a 12000-byte
UTF-8 JSON string. Serialize control replies directly to UTF-8. Each complete
frame fits the existing 16384-byte WebSocket limit. Replay is bounded by the
configured retained event count and uses one event per frame.

Known request field errors return `invalid_message` with an allowlisted
field or null. Other operation errors use a null field and one of
`access_denied`, `request_conflict`, `admissions_closed`, `invalid_cursor`,
or `operation_unsupported`. A failed cancellation can additionally return
`operation_failed` or `outcome_uncertain`. A non-object or malformed JSON frame still
follows the existing `invalid_message` and close 1002 behavior. Existing
frame-size and send-timeout policies apply.

## Optional recent-run discovery

A fresh client that has lost its run ID can request both `durable_runs` and
`durable_run_discovery` in the handshake. Authenticate only after the server
negotiates both. Then send `{"protocol":"avip/1","type":"agent.run.recent"}`.
The request accepts no fields for owner, search, paging or filters. The server
checks the authenticated principal's `discover` and `status` grants.

The reply has the same protocol and type with a `runs` array of up to 16
objects, each containing only `run_id` and `state`. Results are scoped to the
authenticated owner, ordered by creation time descending with descending run ID
as the tie breaker. Active runs and terminal runs within result retention are
eligible. An empty list is a successful reply. Fetching a result or attaching
to a chosen ID requires that operation's own permissions. Older clients may
continue to recover by known ID using only `durable_runs`.

## Local service behavior

`avi.core.durable_runs.DurableRunService` accepts a server-bound
`AuthenticatedConnection` from `LoopbackCredentials.authenticate`.
Use a dedicated SQLite file under a private directory. Test code issues a
grant with `credentials.issue("test-owner", RunPermission, lifetime=900)`
and can revoke it with `credentials.revoke(token)`. The grants live in process.
On restart, issue a new grant for the same principal to retrieve its runs.

The gateway supplies a nonsecret `backend_scope` label unique to the actual
provider origin, profile and credential epoch. It uses exactly the same
label for recovery after a restart and a new label when any part changes.
A replacement scope leaves old handles pending reconciliation. Opaque
handles and the scope label stay in the database. Submission metadata stores
the canonical digest of the task. Retained progress and result text are
stored separately.

Run `await service.recover()` before admitting work on startup and
`await service.recover(startup=False)` to refresh active observations
thereafter. A reserved intent can be dispatched only when its owner retries
with the same validated task. A committed dispatch claim is treated as
uncertain across a crash until the adapter supplies affirmative evidence.
Recovery performs lookups through the backend. A handle receipt can confirm
acceptance while the task remains `submitted`. A lookup miss or incompatible
scope leaves the active observation in place and records pending reconciliation.

Run `await service.maintain()` periodically. Default execution TTL is 600
seconds from reservation. An unclaimed expired run closes locally. A
dispatched expired run records expiry and requests verified remote
cancellation once if supported. An acknowledgement leaves the run active
until terminal evidence arrives. Unresolved active records remain available
for reconciliation. Terminal text and replay events are retained for 3600
seconds from terminal observation. The last 128 events are retained at most
per run. SQLite retains deduplication tombstones for 86400 seconds from
terminal observation. A pruned cursor returns the latest snapshot plus resume
cursor. An expired result returns `retained: false` with a null result.

## Legacy cutover hook

Use one `LegacyCutover(store)` for the single-process loopback gateway. Wrap
each legacy session adapter with `cutover.track(adapter)` at startup and pass
`legacy_cutover=cutover` to `DurableRunService` when preparing opt-in
admissions. `cutover.begin()` closes legacy admissions. Call
`await cutover.enable_durable()` to inspect existing work, or pass
`cancel=True` to request legacy cancellations. Pending submissions and
unconfirmed outcomes block the switch. Terminal evidence is recorded in
SQLite before durable admissions open. `await cutover.rollback()` closes
durable admissions and keeps owner-authorized recovery and reads available.

The default gateway has no durable operations. The standalone hook and
SQLite service are covered by local tests. Real Hermes integration,
privileged gateway registration and the deployed legacy cutover rehearsal
remain release checks.
