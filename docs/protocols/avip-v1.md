# AVIP v1 protocol outline
Status: **Draft — the session handshake is served on `/ws/avip`. Media framing,
the remaining control and event messages, and runnable client conformance are
not implemented. `/ws/avip` is not wire-compatible with the legacy `/ws` JSON
control protocol. M0 is not complete.**

## Intent
A device-neutral contract with (1) media frames, (2) control commands, (3) asynchronous events. WebSocket is first transport; future transports must preserve semantics without dictating wire framing.

## Endpoint
AVIP v1 handshake messages use `GET /ws/avip`. The legacy `/ws` path is unchanged:
it still sends `{type: ready, agent, voice}` and accepts `submit`, `status`,
`ping`, `stop_playback` and `cancel_response`. There is no negotiated upgrade on
`/ws`. That socket speaks first, so it cannot wait to classify a client frame,
and sniffing two JSON dialects on one connection would risk treating a legacy
message as AVIP. Clients select the protocol by path. No WebSocket subprotocol
is required.

`/ws/avip` checks the same localhost origins as `/ws`. That check is defense in
depth, not authentication. The path does not construct an agent adapter and does
not submit runs.

## Proposed JSON envelope
```json
{"protocol":"avip/1","type":"session.start","request_id":"req-001","session_id":null,"device_id":"browser-demo-001","capabilities":{"audio_input":true,"audio_output":true,"camera":false,"display":false}}
```
This browser example advertises audio only. Capability presence is not permission
for physical actuation: M4 supports simulated actions and an optional ROS 2
**mock action** lifecycle only, with no motors or physical robot control in v0.1.
Any future device action requires independent device-side authorization and safety
checks; see [ADR-010](../adr/010-robot-client-integration.md).

The session-start shape above is validated by the handshake contract and accepted
on `/ws/avip`. Other proposed control types are session.end, playback.stop,
response.cancel and agent.run.cancel; proposed events include transcript.delta,
agent.run.status, response.interrupted and error. Those other messages are not
implemented. After a session is ready, a JSON object whose type is not
`session.start` returns `unsupported_message_type` and leaves the ready session
in place.

## Initial session handshake contract

`avi.protocols.avip` validates already-decoded JSON and builds replies. It does
not open sockets, authenticate clients or connect providers. `AvipSession` is the
WebSocket driver: it reads one text frame at a time, calls those helpers, and
discards the session when the socket ends.

- `protocol` must be exactly `avip/1`; there is no silent version downgrade.
- `type` must be `session.start`. This parser handles only the initial handshake.
- All six envelope fields in the example are required; unknown envelope fields are
  rejected. `session_id` must be null: starting a session cannot reattach to a run.
- `request_id` and `device_id` are 1–128 ASCII characters matching
  `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. A device ID is a label, never proof of identity
  or ownership. The gateway allocates the session ID using the same format.
  Clients must treat it as opaque. The development gateway currently emits `s`
  plus 32 lowercase hex digits.
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
  must remain false until corresponding integrations are enabled. The development
  gateway enables none of them, so `/ws/avip` answers with every capability false.
- The synthetic [handshake fixtures](../../tests/fixtures/avip-v1-handshake.json)
  negotiate against audio support so browser and headless examples can share one
  expectation. That availability is test configuration. It is not what `/ws/avip`
  advertises.

For example, a gateway that really did support both audio directions would reply:

```json
{"protocol":"avip/1","type":"session.ready","request_id":"req-001","session_id":"session-001","capabilities":{"audio_input":true,"audio_output":true,"camera":false,"display":false,"robot_actions":false}}
```

The development gateway, with nothing enabled, replies with the same shape and
every capability false. `session-001` above illustrates the pure helper; live
ids come from the gateway allocator.

### Wire errors

Failures use a server-authored envelope. It contains the server's protocol,
`type: error`, an allowlisted `code`, and an allowlisted `field` or null. It
does not copy `request_id`, `device_id`, `session_id`, capabilities, exception
text, or raw frame bytes. A client correlates the envelope with the message it
just sent on this connection.

```json
{"protocol":"avip/1","type":"error","code":"invalid_message","field":null}
```

`code` is `invalid_message`, `unsupported_protocol`, `unsupported_message_type`
or `session_already_started`. `field`, when present, is one of `protocol`,
`type`, `request_id`, `session_id`, `device_id` or `capabilities`. Any other
code or field name is replaced before it is written: unknown codes become
`invalid_message` with a null field. `session_already_started` is raised by the
connection driver. The parser does not raise it. The shared fixture file lists
these envelopes under `wire_errors`.

### Connection lifecycle

1. After the origin check, the server accepts the socket and waits for one text frame. Silence until the shared session idle limit (currently 300 seconds) closes with code 1001 and reason `Idle timeout`. No session is allocated.
2. A valid `session.start` allocates a new session id, negotiates capabilities against support the process actually has enabled, and sends `session.ready`. The id is stored only on that connection.
3. The socket stays ready until it ends. This path has no agent run, voice response or media queue. `active_sessions` counts accepted sockets on `/ws` and `/ws/avip` in this process. It is not a session table.
4. Disconnect, idle timeout, or a fatal protocol close drops the id. Nothing is written to disk. A later connection starts over with `session_id: null` and receives a new id. The previous id is not a lookup key. Legacy `/ws` mock runs stay on their own sockets and still end when those sockets end. `/ws/avip` does not cancel, persist or resume them.
5. A second JSON object whose `type` is `session.start` is `session_already_started`, whatever the other fields contain. The driver does not parse it as a new session and does not replace the id. It then closes the socket.
6. Any other failure of the first frame closes the socket. A retry is a new connection.
7. Text that is not a JSON object is `invalid_message` and closes with code 1002 and reason `Protocol error`. A binary frame is `invalid_message` and closes with code 1003 and reason `Unsupported data`. The bytes are not decoded, stored or forwarded. Binary media framing is still an open decision; this close is the stand-in until that contract exists.
8. After ready, a JSON object that is not a session start returns `unsupported_message_type` with `field: type` and the ready session stays up.
9. A send that exceeds the shared send limit (currently 5 seconds) closes with code 1013 and reason `Send timeout`. The ready or error envelope may not have been delivered. A missing or foreign origin closes with code 1008 and an empty reason, before accept, and produces no AVIP envelope.

Close reasons are fixed strings. They do not contain client data. There is no replay cursor and no reconnect handshake.

## Open design decisions (M0)
- Media codec, sample rate, chunk duration, frame header, timestamps, sequence numbers and endianness.
- Binary audio framing and routing; explicit backpressure, queue drop policy, jitter behavior. Until that contract exists, `/ws/avip` closes on binary frames.
- AuthN/AuthZ of devices and robot actions; when and how remote runs can be reattached. The handshake above does not define that policy.
- Trace context and timestamp definitions; measure cross-host latency without unsynchronized clock subtraction.
- Status/progress model for ROS 2 actions and cancellation acknowledgments.
- VoiceSession, VoiceResponse and AgentRun state machines beyond this connection's start, ready and closed rules.

## Conformance
Web and Python SDKs must pass shared mock-gateway contract tests. Those runnable clients are not in this slice. M4 validates a headless simulator and minimal ROS 2 bridge. The existing `submit` / `status` / `ping` protocol remains legacy until a deliberate migration; a client cannot assume either socket accepts the other's messages.
