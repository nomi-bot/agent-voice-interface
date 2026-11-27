# Current gateway architecture

The [target architecture](architecture/overview.md) defines the ports. Browser
and simulated robot clients use the same AVIP route. The optional ROS 2 bridge
runs on the client and maps synthetic observations to in-memory actions.

A connection to `/ws/avip` negotiates media and optional controls. A configured
Gemini Live mount receives uplink PCM, resamples downlink audio and propagates
turn interruptions. The mock mount mirrors PCM. The connection owns its
session ID, response identifiers, media queues and provider resources. Closing
the connection releases them.

The [local launcher](../README.md#run-the-local-hermes-gateway) binds a
single-owner `DurableRuntime` to the FastAPI application. Each authenticated
socket uses its own grant to call the shared run service. Voice tool calls and
explicit AVIP submissions pass the same approved-query and owner checks.
SQLite stores run ownership, dispatch information and bounded event history.
The voice journal correlates tool calls across retries. On startup, the launcher
reconciles known handles before admitting work. An ambiguous dispatch stays
unresolved until the backend supplies evidence. The pinned Hermes adapter
supports polling by handle; a missing handle limits reconciliation.

A new socket gets a new voice session and authenticates again for durable
access. Clients can use an existing ID or negotiate discovery, select a recent
run and replay from a cursor. Playback stop, response cancel and authorized
task cancellation have distinct scopes. See the [durable extension](protocols/avip-durable-runs-v1.md)
and [session lifecycles](architecture/session-lifecycle.md).

On the legacy `/ws` route, `ControlSession` owns a receiver, sequential agent
worker and writer. Bounded queues and deadlines limit outstanding work. Ping
and local playback replies can overtake agent operations. In the default
configuration, disconnect cancels that socket's mock tasks. During durable
cutover, the launcher fences legacy admission. It keeps old runs under their
original ownership and records confirmed outcomes before opening durable
submissions.

## Protocol (initial control plane)

Client JSON objects: submit(request), status(run_id), cancel_task(run_id), ping,
stop_playback, cancel_response. Server types: ready, run, pong, stop_playback,
unsupported, error. `/ws` still has no binary audio path and no microphone
implementation. AVIP media framing lives in `avi.protocols.media`. The legacy
control session does not call it. `/ws/avip` does, for binary messages after a
ready session with audio input negotiated. An AVIP `session.start` sent to
`/ws` is an unknown legacy message. It does not open an AVIP session.
Malformed messages produce a generic error; runs cannot be accessed across sessions.

## Observability

The gateway counts active sockets. Voice telemetry records media timing and
drops, and the mock socket soak reports connections, frames, reconnects and
process-level resources. Each measurement uses its own clock. Physical
microphone-to-speaker latency and provider cost require separate evidence.
See [telemetry](voice-telemetry.md) and [release qualification](roadmap/delivery/release.md).
