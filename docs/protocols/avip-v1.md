# AVIP v1 protocol outline
Status: **Draft — `/ws/avip` serves the session handshake and, once audio
input is negotiated, binary media frames. The development gateway advertises
no capabilities unless `AVIP_MOCK_MEDIA=1`, which enables the M0 PCM mirror
for every client. The remaining control and event messages, and the
VoiceSession / VoiceResponse / AgentRun state machines, are not implemented.
`/ws/avip` is not wire-compatible with the legacy `/ws` JSON control protocol.
M0 is not complete.**

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
WebSocket driver: it reads one frame at a time, calls those helpers for text,
passes binary messages to `unpack_frame` after the session is ready, and
discards the session when the socket ends. Device labels do not select a path.

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
  `AVIP_MOCK_MEDIA=1` is the one development exception: it advertises audio input
  and audio output together, for every client, and still advertises no camera,
  display, or robot actions.
- The synthetic [handshake fixtures](../../tests/fixtures/avip-v1-handshake.json)
  negotiate against audio support so browser and headless examples can share one
  expectation. Conformance tests apply that availability on the shared route.
  It is not the default process configuration.

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

`code` is `invalid_message`, `unsupported_protocol`, `unsupported_message_type`,
`session_already_started`, `invalid_frame`, `unsupported_codec`,
`frame_too_large`, `sequence_anomaly`, `timestamp_regression`, or `queue_full`.
`field`, when present, is one of `protocol`, `type`, `request_id`, `session_id`,
`device_id`, `capabilities`, `magic`, `version`, `direction`, `codec`,
`reserved`, `sequence`, `timestamp_us`, `payload`, or `header`. Any other code
or field name is replaced before it is written: unknown codes become
`invalid_message` with a null field. `session_already_started` is raised by the
connection driver. The parser does not raise it. The handshake fixture lists
these envelopes under `wire_errors`.

### Connection lifecycle

1. After the origin check, the server accepts the socket and waits for the first frame. Silence until the shared session idle limit (currently 300 seconds) closes with code 1001 and reason `Idle timeout`. No session is allocated. A binary first frame is rejected as in step 7 and is not decoded.
2. A valid `session.start` allocates a new session id, negotiates capabilities against support the process actually has enabled, and sends `session.ready`. The id is stored only on that connection.
3. The socket stays ready until it ends. This path has no agent run and no voice response. It does keep a connection-scoped media queue once the session is ready. `active_sessions` counts accepted sockets on `/ws` and `/ws/avip` in this process. It is not a session table.
4. Disconnect, idle timeout, or a fatal protocol close drops the id and the queue. Nothing is written to disk. A later connection starts over with `session_id: null` and receives a new id. The previous id is not a lookup key. Legacy `/ws` mock runs stay on their own sockets and still end when those sockets end. `/ws/avip` does not cancel, persist or resume them.
5. A second JSON object whose `type` is `session.start` is `session_already_started`, whatever the other fields contain. The driver does not parse it as a new session and does not replace the id. It then closes the socket.
6. Any other failure of the first frame closes the socket. A retry is a new connection.
7. Text that is not a JSON object is `invalid_message` and closes with code 1002 and reason `Protocol error`. A binary frame before `session.ready`, or any binary frame when `audio_input` was not negotiated, is `invalid_message` and closes with code 1003 and reason `Unsupported data`. Those bytes are not decoded, stored, or forwarded.
8. After ready, a JSON object that is not a session start returns `unsupported_message_type` with `field: type` and the ready session stays up. Text is never parsed as a media frame.
9. A send that exceeds the shared send limit (currently 5 seconds) closes with code 1013 and reason `Send timeout`. That limit covers JSON envelopes and mirrored media frames. The frame may not have been delivered. A missing or foreign origin closes with code 1008 and an empty reason, before accept, and produces no AVIP envelope.
10. After ready, with `audio_input` negotiated, each binary message is one complete frame passed to `unpack_frame` and then `MediaDelivery.submit` on the uplink queue. Clients may send `audio_uplink` only. An `audio_downlink` frame from the client is `invalid_frame` with `field: direction` and closes with code 1002 and reason `Protocol error`. Other media `ProtocolError`s use the same close, after the error envelope. `queue_full` is the exception: the envelope is sent and the socket stays open. The refused frame does not advance the uplink sequence.

Close reasons are fixed strings. They do not contain client data. There is no replay cursor and no reconnect handshake.

## Binary media frames

`avi.protocols.media` packs and unpacks one complete audio frame and applies a
bounded delivery policy. It does not read sockets, route audio to a provider,
or reassemble partial messages. `/ws/avip` passes each complete binary message
to `unpack_frame` and leaves text messages on the JSON control plane. One
buffer is one frame.

The M0 profile is mock PCM, chosen so a browser-shaped client and a headless
Python client can share fixtures before a provider format is measured. It is
not a Gemini Live format. `session.start` does not yet negotiate sample rate
or codec; version byte 1 means this profile.

| Field | Value |
| --- | --- |
| Codec | `pcm_s16le` (codec id 1): signed 16-bit little-endian samples |
| Rate and channels | 16000 Hz, mono |
| Nominal chunk | 20 ms, 640 payload bytes. Receivers must also accept any other even length from 2 through 8192 bytes |
| Endianness | Multi-byte integers are little-endian. The magic is four raw bytes, not an integer |
| Directions | 1 = `audio_uplink` (client capture toward AVI), 2 = `audio_downlink` (AVI audio toward the client) |

Header layout, 24 bytes, then the payload:

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 4 | Magic `AVIP` (`41 56 49 50`) |
| 4 | 1 | Version `1` |
| 5 | 1 | Direction |
| 6 | 1 | Codec |
| 7 | 1 | Reserved, must be `0` |
| 8 | 4 | Sequence, `uint32` |
| 12 | 8 | `timestamp_us`, `uint64` |
| 20 | 4 | Payload length, `uint32`, must equal the number of bytes that follow |
| 24 | N | PCM payload |

`timestamp_us` is the sender's media clock for that direction, in microseconds,
from an epoch the sender chooses (often 0 on the first frame). It is not a wall
clock. Compare it only with earlier timestamps on the same direction of the same
stream. Do not subtract client and gateway timestamps, and do not subtract
uplink from downlink.

Each direction has its own sequence. The first frame must use sequence 0. Every
later frame must be the previous sequence plus one, modulo 2^32
(`next_sequence(0xFFFFFFFF) == 0`). A duplicate, gap, or rewind is
`sequence_anomaly` and does not advance the stream. There is no resync.
Timestamps do not wrap; a smaller `uint64` is `timestamp_regression`.

Validation raises the same `ProtocolError` as the handshake. Codes are
`invalid_frame`, `unsupported_protocol`, `unsupported_codec`, `frame_too_large`,
`sequence_anomaly`, `timestamp_regression`, and `queue_full`. The optional
`field` is a fixed token such as `magic`, `version`, `payload`, or `sequence`.
Those codes and fields are on the `/ws/avip` wire allowlist listed above.
Buffers larger than 8216 bytes are `frame_too_large` before the header is read.
Error text does not include client bytes, sequence numbers, or declared lengths.
A non-zero reserved byte, an unknown direction, a payload that is empty, odd, or
a different length than the header, and any codec other than `pcm_s16le` are
rejected. Unknown codecs are not stored or echoed.

Language-neutral byte vectors live in
[avip-v1-media.json](../../tests/fixtures/avip-v1-media.json).

### Bounded delivery

`MediaDelivery` keeps a separate FIFO for each direction. The default and
maximum depth is 8 frames (160 ms at the nominal 20 ms chunk). `offer` on a
full queue raises `queue_full` and does not drop the oldest frame or the frame
being offered. Sequence state advances only after a frame is queued, so a
refused frame keeps its sequence and can be retried unchanged once the consumer
has taken a frame. Uplink pressure does not consume downlink capacity.

`clear` is an explicit flush. It discards queued frames and does not reset the
sequence. Overflow never flushes. This queue does not wait, reorder, or conceal
gaps. Jitter buffering is not part of M0: a missing sequence is an error, not a
concealable loss. Playback delay and drop-oldest under measured latency remain
later work. On the socket, `queue_full` is an error envelope and does not close
the connection. The control plane's existing reply-queue close is a separate limit.

### Mock mirror

When the negotiated capabilities include `audio_output`, each uplink frame that
was queued is also mirrored as one `audio_downlink` frame. The gateway assigns
that frame's sequence, starting at 0, and its timestamp. The timestamp is the
gateway media clock: it starts at 0 and advances by
`samples * 1_000_000 // 16000` microseconds, where `samples` is the payload
length divided by 2. The PCM bytes are copied. This mirror is how a client
checks framing. It is not a voice provider, and the two clocks must not be
subtracted.

The uplink frame stays in the uplink queue. The mirror does not consume it.
With no provider attached, the eighth accepted frame fills the queue and the
next uplink returns `queue_full`. Retrying that same sequence returns
`queue_full` again. A later sequence is `sequence_anomaly`. `AVIP_MOCK_MEDIA=1`
turns this mirror on for every client by advertising audio input and audio
output. The default development gateway advertises neither, so binary frames
still close with 1003.

## Open design decisions (M0)
Settled for this slice: mock PCM profile, header layout, endianness, sequence
and timestamp rules, payload bounds, reject-newest backpressure, socket
attachment, and wire errors for media failures including `queue_full`. Still open:

- Route framed audio through the gateway to a voice provider. The mirror above does not do that, and it does not drain the uplink queue.
- Negotiate a measured codec and sample rate in the handshake. Real-time resampling, jitter buffering, and concealment wait on that measurement (M1) and on full-duplex recovery (M3).
- AuthN/AuthZ of devices and robot actions; when and how remote runs can be reattached. The handshake above does not define that policy.
- Trace context beyond the media clock defined above, and a way to measure
  cross-host latency without subtracting unsynchronized clocks.
- Status/progress model for ROS 2 actions and cancellation acknowledgments.
- VoiceSession, VoiceResponse and AgentRun state machines beyond this connection's start, ready, media, and closed rules.
- The remaining control and event messages (`session.end`, `playback.stop`, `response.cancel`, `agent.run.cancel`, transcripts, and run status).

## Conformance
A browser-shaped client and a headless Python client both speak this socket.
The browser-shaped client sends JSON text and binary frames using the layout in
[browser-avip-client.js](../../tests/fixtures/browser-avip-client.js). It does
not call `pack_frame`. The headless client does, via `avi.protocols.media`.
Shared tests run both against one `/ws/avip` route: handshake, `session.ready`,
then a media round-trip on the mock mirror. Device id is only a label. M4
validates a headless simulator and minimal ROS 2 bridge. The existing `submit`
/ `status` / `ping` protocol remains legacy until a deliberate migration; a
client cannot assume either socket accepts the other's messages.

Run the suite from a Python 3.12 environment with the project installed:

```sh
python3.12 -m unittest discover -s tests -v
python3.12 -m unittest discover -s tests -p test_avip_conformance.py -v
```

Leave `AVIP_MOCK_MEDIA` unset for that run. The conformance tests enable the
shared fixture's audio availability themselves. To paste the browser fixture
into a page on `http://127.0.0.1:8000` and see a mirrored downlink frame, start
the gateway with the variable set:

```sh
AVIP_MOCK_MEDIA=1 python3.12 -m uvicorn avi.main:app --host 127.0.0.1 --port 8000 --ws-max-size 16384 --ws-max-queue 16
```
