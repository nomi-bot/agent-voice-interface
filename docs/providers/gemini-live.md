# Gemini Live provider (JEV-23)

`avi.providers.gemini_live.GeminiLiveProvider` implements the existing
`VoiceProvider` port. The development gateway does not construct it.
`/ws/avip` still uses `MockMediaSink` and the 16 kHz PCM mirror.
`GET /health` still reports `voice: not_implemented` for that process.

The compatibility spike remains `avi.providers.gemini_live_spike` and
[ADR-011](../adr/011-gemini-live-compatibility.md). This page is the adapter
that follows that spike. It does not add a Hermes contract or any physical
actuation.

## What the port does

`VoiceProvider` and `VoiceEvent.kind` are unchanged:

| Method | Adapter behavior |
| --- | --- |
| `start` | Opens one Developer API Live session when `GEMINI_API_KEY` and `GEMINI_LIVE_MODEL` are both set. `vertexai=False`. No model id is embedded. |
| `send_audio(pcm)` | 16 kHz mono PCM s16le, the AVIP uplink profile. Declared as `audio/pcm;rate=16000`. The method still has no sample-rate argument. |
| `events` | One consumer. `audio` payloads are 16 kHz PCM s16le. |
| `tool_result(call_id, result)` | `send_tool_response` for a call the session has already emitted. |
| `cancel_response` | `send_client_content(turns=None, turn_complete=True)`, which the Live API uses as an interrupt, plus a local flush of queued downlink audio. This does not cancel an agent run. |
| `close` | Cancels the uplink and downlink tasks and closes the socket. Idempotent. |

`submit_text` exists on the class for the gated smoke. It is not part of
`VoiceProvider`, and the gateway does not call it.

Legacy `/ws` still answers `cancel_response` with `unsupported`. Wiring that
control message to this provider is not part of this change.

## Interface diff against the spike

ADR-011 listed signals the port does not name. They are mapped onto the
existing event kinds. Payloads are dicts except `audio`, which is bytes.

| Live signal | `VoiceEvent` |
| --- | --- |
| Downlink PCM | `audio`. 24 kHz input is resampled to 16 kHz. A later chunk at a different rate is `error` / `sample_rate_changed` and the session closes. |
| `server_content.interrupted` | `interrupted` with `{"source": "server"}`. Queued downlink audio is dropped. |
| `cancel_response` | `interrupted` with `{"source": "cancel"}`. |
| `function_calls` | One `tool_call` per call: `id`, `name`, `args`. |
| `tool_call_cancellation` | `error` / `tool_call_cancelled` and the ids. Those calls are forgotten. This is not agent-run cancellation. |
| `turn_complete` | `turn_complete`. The payload includes `reason`, `generation_complete`, `interaction_status`, and `transcript_present`. The transcript text is not copied out of the SDK message. |
| `GoAway` | `error` / `go_away`, then the session is closed. |
| Session resumption update | Counted only as `resumption_updates` when `resumable` is true. `new_handle` is never read and never stored. |
| Connect or stream failure | `error` / `provider_error` with a redacted `detail`, or `ProviderSessionError` from `start`. |

`generation_complete` and `interaction_status` are not separate event kinds.
They ride on the next `turn_complete`.

Session resumption is not enabled on the provider config. The spike still
enables it for its own measurement. This adapter closes on `GoAway` instead of
resuming. Automatic renewal is deferred.

## PCM

Live input is 16 kHz. Live output is 24 kHz. AVIP playback is 16 kHz.
`StreamingResampler` keeps the fractional read cursor across chunks. Three
seconds of 24 kHz PCM pushed in 20 ms pieces matches one push and yields
exactly three seconds at 16 kHz. `reset` drops the tail so the next turn does
not blend with audio that was interrupted.

The browser helper `resampleS16leToAvip` is still a one-shot conversion.
`S16leResampler` in `clients/browser/pcm.js` is the streaming equivalent for a
client that receives 24 kHz chunks. The voice page still refuses to play 24 kHz
bytes as 16 kHz. The provider is what emits 16 kHz for a future gateway path.
The mock mirror does not call either resampler.

Uplink inside the provider is capped at 8 chunks. A full queue drops the newest
chunk and counts `dropped_uplink_chunks`. It does not insert silence. Downlink
events are capped at 8 audio events. A full audio queue drops the newest event.
Control events are capped at 32; overflow closes the session with
`event_overflow`. One uplink chunk may not exceed 32,000 bytes. A tool result
may not exceed 16 KiB of JSON.

## Uplink clock after a dropped frame

Browser capture drops frames when the WebSocket send budget is full, and it
drops the oldest frames when one callback produces more than 8. Those drops do
not advance the AVIP sequence. They do advance the sender media clock by 20 ms
per dropped nominal frame (`noteCaptureGap` / `UplinkClock.note_capture_gap`).
The next frame that is sent keeps the next sequence number and a later
timestamp. Silence is not inserted, because the queue is already full.
`skippedMediaUs` counts that gap. A contiguous sequence is not continuous
speech. The headless client exposes the same clock method. The gateway mirror
clock is unchanged.

## Tools

Every provider session declares one function, `avi_ack`. It takes an empty
object and does not control a device. `behavior` is set explicitly to
`BLOCKING` or `NON_BLOCKING`. The default is `NON_BLOCKING`, which the 3.8 Live
model page describes as the default and which extended thinking requires.
`BLOCKING` remains available for a caller that selects a synchronous model.
Omitted behavior is not used, because ADR-011 records that the model page and
the tools guide disagree about the default.

`tool_result` sends `send_tool_response`. For `NON_BLOCKING`, the SDK field
`FunctionResponse.scheduling` is `WHEN_IDLE`. For `BLOCKING`, scheduling is
omitted. The adapter does not send `INTERRUPT`, and it does not send the
`INTERRUPTED` token from the 3.8 model page. That spelling is still unresolved.
The tools guide's placement of `scheduling` inside the response JSON is not
used; the typed SDK field is.

`run_tool_handshake` opens two short sessions, one per behavior, asks for
`avi_ack`, waits 1.5 s to see whether audio arrives before the response, then
sends the tool result. `audio_before_tool_response` is the observation. It is
not a proof that every future call will wait or continue. The report stores
that flag, the configured behavior, and whether `turn_complete` arrived. It
does not store arguments, audio, the key, or a resumption handle.

`run_duplex_roundtrip` sends 100 ms of 16 kHz silence, then one text turn that
asks for a spoken word, and records downlink byte count and milliseconds from
the text send to the first 16 kHz audio event. The text turn is there because
this repository has no speech fixture. Silence is not a conversational prompt.

Neither function runs unless `GEMINI_API_KEY` is set. A key without
`GEMINI_LIVE_MODEL` fails closed. `GOOGLE_API_KEY` does not authorize a call.

This environment had no `GEMINI_API_KEY`. The live duplex round trip and the
tool handshake were skipped. `BLOCKING` versus `NON_BLOCKING` on the selected
model is therefore still not a measured result. The offline tests cover the
handshake shape with a fake session: the tool call is observed, the response
dict is the one `send_tool_response` would send, and scheduling is present only
for `NON_BLOCKING`.

## Negative cases

- Missing or blank `GEMINI_API_KEY`: `ProviderNotConfigured`, no socket.
- Key set, model missing or not a single token: `ModelUnconfigured`. The value is not echoed.
- Connect failure, `GoAway`, and `close` during connect: tasks are cancelled and `close` is called on the session that was opened.
- Odd, empty, or oversized PCM is rejected.
- `send_audio` before `start`, a second `start`, and a second `events` consumer raise.
- Unknown tool id, a non-dict result, and an oversized result are rejected and not sent.
- Server `interrupted` does not emit the audio that arrived on that message.
- A resumption handle on a `GoAway` message is not retained in the provider or the event.
- Error text is passed through `redact` so the API key is replaced with `[redacted]`.

## Browser assets

`GET /voice` reads an allowlist. A wheel built from this repo includes those
files at `avi/_browser/`. A source checkout uses `clients/browser` when the
packaged directory is incomplete. A complete packaged directory wins. The route
still rejects names outside the allowlist. `hatchling` is a dev dependency so
the unit test can build that wheel without a separate download during the test.

## Commands

Offline, from a Python 3.12 environment:

```sh
python -m pip install -e '.[dev,gemini-live]'
python -m unittest discover -s tests -v
```

On this workspace, CPython 3.12.3, that run was **181 tests, 179 passed, 2
skipped**. The skips are `test_live_round_trip_when_configured` and
`test_live_duplex_and_tool_handshake_when_configured`, both because
`GEMINI_API_KEY` was unset. The spike module exits 2 in that case.

With a key and a model code from ADR-011, the same discovery runs the live
tests. There is no separate command required. To run only the adapter module's
gated checks:

```sh
python -m unittest tests.test_gemini_live_provider.ProviderContractTests.test_live_duplex_and_tool_handshake_when_configured -v
```

CI is the existing Python checks workflow: Ubuntu and macOS, Python 3.12 and
3.13, `pip install -e '.[dev,gemini-live]'`, then `unittest discover`. This
change does not add a credential to CI. The live tests skip there.

## Latency

No credential was available here, so no Live round trip was timed. The duplex
report's `first_audio_ms` is the only latency field, and it is filled only when
that gated test runs. It is the time from the text turn to the first resampled
audio event on one machine. It is not a cross-host or product SLO.

## Security

- The key stays in the process environment. This repository does not load
  `.env` files. The provider reads `GEMINI_API_KEY` only.
- Reports and `repr` do not include the key, PCM, or a resumption handle.
- Nothing in this adapter writes those values to disk.
- Tool arguments are copied into the in-memory `tool_call` event because
  `tool_result` cannot be built without them. They are not logged.
- Transcript text is not copied into `VoiceEvent`.
- The provider does not call an agent and does not actuate hardware.
- The gateway bind and origin allowlist are unchanged.

## Deferred

- Mounting the provider on `/ws/avip`. The mock mirror and `MockMediaSink` stay
  the gateway path.
- Automatic session renewal across `GoAway`.
- Choosing between the SDK scheduling token `INTERRUPT` and the model-page
  token `INTERRUPTED`. Neither is sent.
- A credentialed measurement of `BLOCKING` versus `NON_BLOCKING`, and of
  `first_audio_ms`, on the operator's model.
- Copying transcript text onto an event.
- Connecting legacy `/ws` `cancel_response` to this provider.
- Telemetry work from a later issue, if it is not already on `main`. This
  change does not add a telemetry channel. The browser page still records the
  same four local timing names, plus `skippedMediaUs` for dropped uplink audio.
