# AVIP v1 protocol outline
Status: **Draft — session-handshake validation and negotiation implemented as a
standalone contract; no AVIP endpoint or media path yet. Not wire-compatible with
the current JSON control protocol.**

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

The session-start shape above is validated by the initial handshake contract.
Other proposed control types are session.end, playback.stop, response.cancel and
agent.run.cancel; proposed events include transcript.delta, agent.run.status,
response.interrupted and error. These other messages are not implemented yet.

## Initial session handshake contract

`avi.protocols.avip` validates already-decoded JSON and builds a `session.ready`
reply. It is transport-independent and does not open sessions, authenticate clients,
connect providers or change the existing `/ws` endpoint.

- `protocol` must be exactly `avip/1`; there is no silent version downgrade.
- `type` must be `session.start`. This parser handles only the initial handshake.
- All six envelope fields in the example are required; unknown envelope fields are
  rejected. `session_id` must be null: starting a session cannot reattach to a run.
- `request_id` and `device_id` are 1–128 ASCII characters matching
  `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. A device ID is a label, never proof of identity
  or ownership. The gateway allocates the session ID using the same format.
- `capabilities` is an object of strict boolean values. Known names are
  `audio_input`, `audio_output`, `camera`, `display` and `robot_actions`; omitted
  names default to false. Unknown optional boolean names are ignored and never
  echoed as enabled features. Unknown envelope fields remain errors.
- Audio directions are relative to the client: input means client capture sent to
  AVI, output means AVI audio played by the client. A feature is negotiated only
  when both the client requests it and the gateway advertises actual support.
  Capability presence is not authorization. Physical actuation is excluded.
- A `session.ready` reply echoes the validated request ID and includes the
  gateway-assigned session ID and all five negotiated boolean capabilities.
  An empty capability set is valid for a control-only client. Gateway defaults
  must remain false until corresponding integrations are enabled.

For example, a future mock gateway supporting both audio directions would reply:

```json
{"protocol":"avip/1","type":"session.ready","request_id":"req-001","session_id":"session-001","capabilities":{"audio_input":true,"audio_output":true,"camera":false,"display":false,"robot_actions":false}}
```

Validation raises `ProtocolError` with a stable `code` and optional `field`:
`invalid_message`, `unsupported_protocol` or `unsupported_message_type`. Errors do
not echo client-supplied values. Wire error envelopes, close/retry behavior and
duplicate-request handling remain decisions for WebSocket integration.

The language-neutral [handshake fixtures](../../tests/fixtures/avip-v1-handshake.json)
cover browser, headless and control-only requests plus invalid requests. Their
audio availability is synthetic test configuration, not a live provider claim.

## Open design decisions (M0)
- Media codec, sample rate, chunk duration, frame header, timestamps, sequence numbers and endianness.
- Binary audio framing and routing; explicit backpressure, queue drop policy, jitter behavior.
- Integrate the initial handshake and its errors with WebSocket; define wire
  error envelopes, duplicate-request rules and reconnect/replay semantics.
- AuthN/AuthZ of devices and robot actions; when and how remote runs can be reattached.
- Trace context and timestamp definitions; measure cross-host latency without unsynchronized clock subtraction.
- Status/progress model for ROS 2 actions and cancellation acknowledgments.

## Conformance
Web + Python SDKs must pass shared mock-gateway contract tests. M4 validates a headless simulator and minimal ROS 2 bridge. Existing `submit/status/ping` protocol remains legacy until deliberate migration; no silent compatibility assumption.
