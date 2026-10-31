# Current skeleton architecture

This page describes executable behavior. See the [target architecture](architecture/overview.md)
and [canonical roadmap](roadmap/milestones.md) for planned changes.

Browser / future robot → TransportAdapter → VoiceOrchestrator →
VoiceProvider and AgentAdapter. DeviceAdapter describes hardware capabilities.

The control plane uses Python asyncio and FastAPI. Current executable scope is a
local JSON WebSocket with session-owned mock tasks. VoiceProvider and DeviceAdapter
are contracts only; they are not wired into a media path. The orchestrator contains
no Hermes-specific logic. Agent submission returns a run immediately; status and
cancellation are capability-gated. Future adapters must never block the event loop.

`ControlSession` owns a receiver, one sequential agent worker, and one writer in
an asyncio task group. Bounded operation/reply queues keep adapter latency and
transport backpressure from accumulating unbounded work. The receiver handles
`ping` and `stop_playback` directly; they can overtake agent replies. Disconnect,
idle expiry and send failure cancel the sibling tasks before closing the agent.
Operation, send and cleanup deadlines are distinct; an operation timeout returns
an error without being reported as an idle timeout. Async adapters must propagate
cancellation and clean up their own resources, including any remote submission
whose result is unknown after a timeout. Python cannot forcibly stop a coroutine
that suppresses cancellation or blocks the event loop.

Stopping playback, cancelling a model response, and cancelling an agent run are
separate operations. A provider interruption should flush client playback without
implicitly cancelling a run. Target durable agent runs should outlive individual voice sessions;
the mock deliberately cancels session-owned work on disconnect to avoid orphan tasks.
Durable runs require explicit ownership, storage and reconnection policy. All
[M2 exit gates](roadmap/milestones.md#m2-exit-gates) must pass before that behavior
can replace the current session-owned policy.

## Protocol (initial control plane)

Client JSON objects: submit(request), status(run_id), cancel_task(run_id), ping,
stop_playback, cancel_response. Server types: ready, run, pong, stop_playback,
unsupported, error. There is no binary PCM protocol or microphone implementation yet.
Malformed messages produce a generic error; runs cannot be accessed across sessions.

## Observability plan

Only active session counting is implemented (process-local app state). Before
performance claims, instrument monotonic timestamps for first response audio,
gateway ingress/egress, interruption-to-playback-stop acknowledgements, delegation
and task completion. Client and server clocks must not be subtracted without
clock alignment. Track queue depth, underruns, audio drops, CPU and RSS separately.
CPU/RSS are process measures unless experimentally attributed to sessions.
There are no measured latency benchmarks or full-duplex results yet.
