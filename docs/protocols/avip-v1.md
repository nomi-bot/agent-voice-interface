# AVIP v1 protocol outline
Status: **Draft — not implemented or wire-compatible with current JSON control protocol.**

## Intent
A device-neutral contract with (1) media frames, (2) control commands, (3) asynchronous events. WebSocket is first transport; future transports must preserve semantics without dictating wire framing.

## Proposed JSON envelope
```json
{"protocol":"avip/1","type":"session.start","request_id":"req-001","session_id":null,"device_id":"browser-demo-001","capabilities":{"audio_input":true,"audio_output":true,"camera":false,"display":false}}
```
This browser example advertises audio only. Capability presence is not permission
for physical actuation: M4 supports simulated actions and an optional ROS 2
**mock action** lifecycle only, with no motors or physical robot control in v0.1.
Any future device action requires independent device-side authorization and safety
checks; see [ADR-010](../adr/010-robot-client-integration.md).

This is an *illustrative schema*, not a final validated message. Potential control types: session.start, session.end, playback.stop, response.cancel, agent.run.cancel; events include session.ready, transcript.delta, agent.run.status, response.interrupted, error.

## Open design decisions (M0)
- Media codec, sample rate, chunk duration, frame header, timestamps, sequence numbers and endianness.
- Binary audio framing and routing; explicit backpressure, queue drop policy, jitter behavior.
- Protocol version negotiation, capability schema, request IDs, errors and reconnect/replay semantics.
- AuthN/AuthZ of devices and robot actions; when and how remote runs can be reattached.
- Trace context and timestamp definitions; measure cross-host latency without unsynchronized clock subtraction.
- Status/progress model for ROS 2 actions and cancellation acknowledgments.

## Conformance
Web + Python SDKs must pass shared mock-gateway contract tests. M4 validates a headless simulator and minimal ROS 2 bridge. Existing `submit/status/ping` protocol remains legacy until deliberate migration; no silent compatibility assumption.
