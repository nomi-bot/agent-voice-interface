# Architecture

Browser / future robot → TransportAdapter → VoiceOrchestrator →
VoiceProvider and AgentAdapter. DeviceAdapter describes hardware capabilities.

The control plane uses Python asyncio and FastAPI. Current executable scope is a
local JSON WebSocket with session-owned mock tasks. VoiceProvider and DeviceAdapter
are contracts only; they are not wired into a media path. The orchestrator contains
no Hermes-specific logic. Agent submission returns a run immediately; status and
cancellation are capability-gated. Future adapters must never block the event loop.

Stopping playback, cancelling a model response, and cancelling an agent run are
separate operations. A provider interruption should flush client playback without
implicitly cancelling a run. Future sessions should outlive individual agent runs;
the mock deliberately cancels session-owned work on disconnect to avoid orphan tasks.
Durable runs require explicit ownership, storage and reconnection policy.

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
