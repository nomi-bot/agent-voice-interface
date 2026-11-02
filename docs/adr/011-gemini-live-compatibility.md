# ADR-011: Gemini Live API compatibility

**Status:** Proposed. This records the M1/A0 spike. It does not implement a voice provider.

**Date:** 2026-11-01

## Context

M1 needs a real-time voice provider. The reserved adapter in
`avi.providers.gemini_live` raises `NotImplementedError`. Before JEV-23 wires
that adapter into the gateway, the Live API model codes, audio framing, session
renewal, tool schema, and cancellation signals have to be checked against
current Google documentation and the Python SDK that would be pinned.

AVI stays a portable interaction layer. This record does not add robot
actuation, a Hermes contract, or a browser media path. Provider credentials stay
on the server, consistent with [ADR-002](002-transport-topology.md) and
[ADR-005](005-security-boundaries.md). Playback interruption and agent-run
cancellation stay separate operations, consistent with
[ADR-009](009-agent-run-lifecycle.md).

## Sources checked

Retrieved 2026-11-01.

| Source | What it establishes | Stamp on the page |
| --- | --- | --- |
| [Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities) | Model comparison, PCM framing, VAD, interruption, native-audio limits | Last updated 2026-10-29 UTC |
| [Tool use with Live API](https://ai.google.dev/gemini-api/docs/live-api/tools) | Manual function responses, blocking vs non-blocking behavior, scheduling examples | Retrieved 2026-11-01; examples name a 3.8 model while the support table does not |
| [Session management](https://ai.google.dev/gemini-api/docs/live-api/session-management) | Connection lifetime, compression, resumption handles, GoAway | Retrieved 2026-11-01 |
| [WebSockets API reference](https://ai.google.dev/api/live) | Setup, server messages, tool-call cancellation, activity handling | Last updated 2026-10-09 UTC |
| [Models](https://ai.google.dev/gemini-api/docs/models) | Live and non-Live model codes | Last updated 2026-10-28 UTC |
| [Gemini 3.8 Live](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live) | Stable low-latency Live model and migration notes from 3.1 | Latest update September 2026 |
| [Gemini 3.8 Live Extended Thinking](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live-extended-thinking) | Async-only tools and `interaction_status` | Latest update September 2026 |
| [Gemini 3.1 Flash Live Preview](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview) | Legacy preview; synchronous tools only | Latest update March 2026 |
| [Gemini 2.5 Flash Live Preview](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-native-audio-preview-12-2025) | Older preview Live model | Latest update September 2025 |
| [Gemini Enterprise Agent Platform session guide](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/start-manage-session) | Vertex-style session lifetime and resumption | Retrieved 2026-11-01 |
| [Gemini Enterprise Agent Platform best practices](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/live-api/best-practices) | Transparent resumption and example WebSocket error codes | Retrieved 2026-11-01 |
| [google-genai 2.34.0 on PyPI](https://pypi.org/project/google-genai/2.34.0/) | SDK release, Python requirement, removal planned for SDK 3 | Released 2026-10-29 |

The pinned package was installed and inspected with CPython 3.12.3. The smoke
harness still refuses to embed or default a model id.

## What was measured

On 2026-11-01, after AI Studio billing was restored, the project owner ran the
smoke on commit `ea48f67` with `google-genai==2.34.0` and
`GEMINI_LIVE_MODEL=gemini-3.8-live`. The redacted result recorded on PR #9 was
`status=passed model=gemini-3.8-live audio_bytes=34562 transcript=true resumption_handle=true`.
An earlier run the same day failed with API error 1011 (prepayment credits
depleted) and did not print the key.

That result measures one completed Developer API text turn for that model code:
the session produced spoken audio bytes, an output transcript, and a resumption
handle. It does not measure sample rate, latency, resampling into the 16 kHz
AVIP profile, renewal across `GoAway`, or a tool round-trip. The 24 kHz output
rate remains the capabilities-guide contract, not a measured rate.

## What stays documentation-only

The matrix and the limits below are copied from the sources in the table. The
measured turn did not exercise them:

- Tool `behavior` (`BLOCKING` versus `NON_BLOCKING`) and which scheduling token
  the model accepts (`INTERRUPT` on the pinned SDK enum, `INTERRUPTED` on the
  3.8 model page). The smoke registers no tools and fails closed if a tool call
  arrives. A tool handshake is deferred to JEV-23; this spike does not treat
  tool behavior as verified.
- Vertex resumption lifetimes. The Cloud pages disagree with each other and
  with the Developer API's 2-hour handle.
- Connection and session durations (about 10 minutes per connection, 15 minutes
  of audio without compression, 2 minutes with video, 2-hour Developer API
  handles).
- Extended thinking, the 3.1 preview, and the 2.5 preview.

The Live API product is still labeled Preview on the capabilities guide and the
WebSockets reference. The 3.8 Live model page separately calls
`gemini-3.8-live` a stable model code. Both statements are from Google; this
spike does not collapse them into one maturity claim.

## Decision

Use the Gemini Developer API through `google-genai==2.34.0` for the M1
compatibility target. Keep the long-lived API key on the server. Pass
`vertexai=False` and the key explicitly so `GOOGLE_API_KEY`,
`GOOGLE_GENAI_USE_VERTEXAI`, and application-default credentials cannot move
the smoke onto Vertex.

Do not put a model id in Python. Operators set `GEMINI_LIVE_MODEL` to a model
code from this record. The code exercised in the measured turn is the stable
low-latency id on the [Gemini 3.8 Live model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-live):
`gemini-3.8-live`. That page describes it as the default low-latency voice
model, with both blocking and non-blocking function calls. Extended thinking
adds background reasoning and drops blocking calls and scheduling. The 3.1
preview is legacy and synchronous only. The 2.5 preview is access-limited for
new projects. The measured turn does not make those tool claims true for the
provider. JEV-23 still has to confirm function behavior on the selected model.

SDK methods used by the smoke, and intended for JEV-23, are the ones the 2.34.0
changelog says to keep: `send_client_content`, `send_realtime_input`,
`send_tool_response`, and `receive`. `Live.send` and `Live.start_stream` still
exist on 2.34.0 and are marked deprecated. PyPI says SDK 3 removes them. The
pin is exact so this inspection stays reproducible, and it stays below 3.

`google-genai` 2.34.0 requires Python `>=3.11`. AVI requires Python `>=3.12`.
The optional extra is `gemini-live` in `pyproject.toml`. The default install
does not depend on it. CI installs `.[dev,gemini-live]`.

## Decision matrix

Audio framing below is the Live API contract from the capabilities guide, not a
per-model measurement. Input is raw 16-bit little-endian PCM, natively 16 kHz,
declared as `audio/pcm;rate=16000`. The service resamples other declared input
rates. Output is raw 16-bit little-endian PCM at 24 kHz. Native-audio models
accept `AUDIO` as the response modality. Text for the application comes from
output-audio transcription.

| Variant | Documented model code | Channel | Live voice | Tools documented for this variant | Async function calls | Session notes | Spike disposition |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Gemini 3.8 Live | `gemini-3.8-live` | Stable | Yes. Text, images, audio in. Text and audio out. Input limit 131,072. Output limit 65,536. | Function calling and Search grounding. Code execution, Google Maps, URL context, and structured outputs are not supported. | Model page: `NON_BLOCKING` is the default. `BLOCKING` remains available. Scheduling tokens named there are `SILENT`, `WHEN_IDLE`, and `INTERRUPTED`. These tool claims are documentation-only. | Same Developer API limits as the session guide. Omit `thinking_level`. `proactive_audio: false` returns an error. Affective dialog is removed. Session durations are documentation-only. | Text turn measured 2026-11-01 (audio, transcript, resumption handle). Not a code default. |
| Gemini 3.8 Live Extended Thinking | `gemini-3.8-live-extended-thinking` | Stable | Yes. Same input and output classes and token limits as 3.8 Live. | Function calling is async only. Search grounding is supported. | `NON_BLOCKING` only. `BLOCKING` returns a hard error. Function scheduling is not supported. `thinking_level` is `low`, `medium`, or `high`. | `turn_complete` does not mean idle. Read `interaction_status` (`IN_PROGRESS` or `IDLE`). | Poor fit for the M1 latency path and for a single `tool_result` scheduling policy. |
| Gemini 3.1 Flash Live Preview | `gemini-3.1-flash-live-preview` | Preview, legacy | Yes. Same token limits. `thinking_level` includes `minimal`. | Function calling. Search is listed for this preview on the tools page. | Not supported. The model waits for the tool response before it speaks. | Capabilities guide: proactive audio and affective dialog are not supported. | Legacy. The model page says to move to 3.8 Live. |
| Gemini 2.5 Flash Live Preview | `gemini-2.5-flash-native-audio-preview-12-2025` | Preview | Yes. Output token limit 8,192. | Tools page: function calling, synchronous and asynchronous, and Search. | Tools page says both modes. That page's support table does not list the 3.8 models. | Models index: new projects should leave 2.5; access is limited to prior users. | Out of the new AVI path. |
| Gemini 3.8 Flash and the TTS model codes | `gemini-3.8-flash`, `gemini-3.8-flash-tts`, `gemini-3.8-flash-lite-tts`, and the other TTS codes on the models page | Stable or preview | Flash is not a Live API model. TTS models are not a bidirectional Live session. | n/a | n/a | n/a | Out of scope for the voice session. |
| Vertex / Gemini Enterprise Agent Platform | Publisher resource `projects/{project}/locations/{location}/publishers/*/models/*` | Google Cloud | Live API exists. The shared SDK can target it. | The Cloud reference lists tools on setup. This spike did not re-check each model there. | SDK field docs say `FunctionResponse.will_continue` is not supported on Vertex. `FunctionCall.partial_args` and `FunctionCall.will_continue` are not supported on the Gemini Developer API. | The Cloud session guide says both "within 24 hours" and "typically around 10 minutes" for resumption, and says GoAway arrives 60 seconds before a 10-minute connection ends. The Developer API session guide says resumption tokens last 2 hours. Transparent resumption (`last_consumed_client_message_index`) is documented on the Cloud best-practices page and is a field on the pinned SDK. | Out of this spike. The two Cloud lifetime statements disagree, and they disagree with the Developer API. Enabling resumption caches prompts and outputs; the Cloud guide says to leave it off when zero data retention is required. |

## SDK surface inspected on 2.34.0

`AsyncSession` provides `send_client_content`, `send_realtime_input`,
`send_tool_response`, `receive`, and `close`. `send` and `start_stream` are
present and deprecated.

`Behavior` wire values are `UNSPECIFIED`, `BLOCKING`, and `NON_BLOCKING`.
`FunctionDeclaration.behavior` is supported on `BidiGenerateContent`. If it is
omitted, the SDK description says the service keeps its current behavior. That
is why a missing behavior flag is not a portable default: the 3.8 model page
and the tools guide describe different current behavior.

`FunctionResponseScheduling` wire values are `SCHEDULING_UNSPECIFIED`,
`SILENT`, `WHEN_IDLE`, and `INTERRUPT`. The typed field is
`FunctionResponse.scheduling`. Its description says it applies only to
`NON_BLOCKING` calls, is ignored otherwise, and defaults to `WHEN_IDLE`. The
tools guide instead places a `scheduling` key inside the `response` object.
The 3.8 model page names the third token `INTERRUPTED`, which is not a member
of this enum. This spike does not send either form. JEV-23 needs one live tool
round-trip on the selected model before it treats either spelling as accepted.

`ActivityHandling` wire values are `ACTIVITY_HANDLING_UNSPECIFIED`,
`START_OF_ACTIVITY_INTERRUPTS`, and `NO_INTERRUPTION`. The WebSockets reference
says the default is barge-in: the start of activity cuts off the current
response. `NO_INTERRUPTION` leaves the response running.

`TurnCompleteReason` includes safety and rejection codes, including
`MALFORMED_FUNCTION_CALL`, `RESPONSE_REJECTED`, `NEED_MORE_INPUT`, and
`GENERATED_AUDIO_SAFETY`. The credentialed smoke accepts only a missing reason
or `TURN_COMPLETE_REASON_UNSPECIFIED`.

`LiveConnectConfig` on this pin includes `session_resumption`,
`context_window_compression`, `realtime_input_config`, `thinking_config`,
`enable_affective_dialog`, and `proactivity`. The smoke sets audio output,
empty session resumption, and output transcription. It leaves thinking,
affective dialog, and proactivity unset. The 3.8 model page says setting
`proactive_audio` false is an error and that affective dialog has been removed.
The capabilities guide still shows both as opt-in flags for older setup. The
model page is the newer 3.8 contract.

`get_env_api_key` in this pin prefers `GOOGLE_API_KEY` over `GEMINI_API_KEY`
when both are set, and it treats an empty string as missing. The smoke does
not call that helper. It reads `GEMINI_API_KEY` only.

## PCM, sessions, tools, errors, and cancellation

**PCM.** The capabilities guide is the framing source. The smoke's local helper
checks that an input payload is non-empty even-length bytes and tags it
`audio/pcm;rate=16000`. AVIP's M0 profile is 16 kHz PCM s16le
(`avi.protocols.media`). Provider downlink at 24 kHz does not match that
profile. JEV-23 owns resampling or an explicit rate negotiation. This spike
does not resample and does not write audio files.

**Session renewal.** On the Developer API, a connection lasts about 10 minutes.
Without context-window compression, an audio-only session lasts 15 minutes and
an audio-plus-video session lasts 2 minutes. Compression uses a sliding window
and can extend the session. The server sends `GoAway.time_left` before it
terminates the connection as `ABORTED`. Session resumption is configured with
`SessionResumptionConfig.handle`. Updates arrive as `SessionResumptionUpdate`:
keep `new_handle` only when `resumable` is true. Handles are valid for 2 hours
after the last termination. Resumption is not available while the model is
generating or executing function calls; the handle is empty and resuming an
older handle drops data. Native-audio context window is 128k tokens. The smoke
enables resumption and reports only whether a handle arrived. It does not print
the handle.

**Tool schema.** A Live tool is a `function_declarations` entry with `name`,
optional `description`, and either `parameters` (OpenAPI-style schema) or
`parameters_json_schema`. The tools guide says Live does not run automatic
function responses. The client matches `FunctionCall.id` and returns
`FunctionResponse` through `send_tool_response`. Search grounding is a separate
`google_search` tool. Code execution, Google Maps, and URL context are
documented as unsupported on the Live models in this matrix. One server event
can contain several content parts; readers have to walk every part.

**Sync versus async tools.** These limits are the reason JEV-23 cannot treat
`VoiceProvider.tool_result` as one behavior for every Live model:

1. Tools guide: omitted `behavior` runs the call sequentially, and the session
   does not continue until the response arrives. 3.8 Live model page: omitted
   behavior is non-blocking, and blocking is opt-in. Set `behavior` explicitly
   for the selected model.
2. 3.8 Live Extended Thinking accepts only `NON_BLOCKING`. Blocking is a hard
   error. Scheduling is unsupported. After `turn_complete`, keep reading until
   `interaction_status` is `IDLE`, because background work can still emit audio
   or tool calls.
3. 3.1 Flash Live Preview does not support async calls. A slow tool holds the
   spoken reply.
4. `FunctionResponse.will_continue` turns a non-blocking call into a generator.
   The SDK says it is ignored for blocking calls and is not supported on
   Vertex. Finishing without a spoken follow-up requires `scheduling=SILENT`.
5. `FunctionCall.partial_args` and `FunctionCall.will_continue` are present on
   the SDK and marked unsupported on the Gemini Developer API. Do not depend on
   streamed tool arguments.
6. A client interruption sends `toolCallCancellation` with the ids to cancel.
   The WebSockets reference says those calls should not have been executed and
   that clients may have to undo side effects. VAD cancellation also discards
   pending function calls. That signal must not be implemented as
   agent-run cancellation. [ADR-009](009-agent-run-lifecycle.md) keeps the run
   alive across voice interruption. A Live function call is the wrong
   durability boundary for an agent task.

**Streaming errors and cancellation.** There is no Live method named
`cancel_response`. Official cancellation paths are:

- Default `ActivityHandling`: `START_OF_ACTIVITY_INTERRUPTS`. New user activity
  cuts the current response. The server sets `server_content.interrupted`. An
  interrupted turn has no `generation_complete`; the sequence is interrupted,
  then `turn_complete`. Clients drop queued playback on `interrupted`.
- `send_client_content(..., turn_complete=True)` unconditionally interrupts
  active generation on the 3.8, 3.8 Extended Thinking, and 3.1 models.
- `send_realtime_input(audio_stream_end=True)` flushes cached audio when
  automatic VAD is enabled and the microphone pauses for more than a second.
  With automatic VAD disabled, the client sends `activity_start` and
  `activity_end` instead.
- `GoAway`, then connection close as `ABORTED`.
- Closing the WebSocket.
- `turn_complete_reason` values for rejected or unsafe turns.

`send_realtime_input` is optimized for responsiveness and is not ordered
against `send_client_content`. The Vertex best-practices page gives WebSocket
close codes 1000 and 1006 as examples of `APIError`. The Developer API
reference does not publish a close-code catalog. This spike does not treat
forum reports of 1008 or 1011 as a contract.

## Interface diff for JEV-23

`VoiceProvider` today is `start`, `send_audio(pcm)`, `events`,
`tool_result(call_id, result)`, `cancel_response`, and `close`. `VoiceEvent`
kinds are `audio`, `interrupted`, `tool_call`, `turn_complete`, and `error`.

The Live session also needs representations for `go_away`,
`tool_call_cancellation`, `generation_complete`, `interaction_status`,
resumption updates, and 24 kHz downlink versus 16 kHz AVIP uplink. `send_audio`
does not carry a sample rate. `cancel_response` has no single RPC.
`tool_result` cannot assume scheduling is accepted. This spike leaves the port
unchanged and leaves `GeminiLiveProvider` unimplemented.

## Alternatives

Implementing the provider in this change would mix an unverified live run into
the gateway. Calling Vertex from the same smoke would follow a resumption
contract the Cloud pages do not state consistently. Pinning a version range
instead of `2.34.0` would let a later 2.x release change enum wire values
without a failing test. Embedding the 3.8 model code would hide operator
selection. The measured turn supplied that code through the environment.

## Consequences

The compatibility target is reviewable without a credential. CI covers the
skip path, the missing-model failure, PCM checks, message classification, and
the SDK pin. A live turn runs only when both environment variables are set,
and it fails on safety reasons, unexpected tool calls, timeouts, and turns
that end without audio or a transcript. Session handles and API keys stay out
of reports.

JEV-23 still has to resample, map the port, choose an explicit function
`behavior`, confirm the scheduling token with a live tool call, and renew
sessions on `GoAway`. Until those exist, the gateway must not advertise a
working Gemini voice path.

## Review trigger

Replace this record, or add a successor, when JEV-23 finishes the tool
handshake, when `google-genai` 3 removes the deprecated Live methods, or when
Google resolves the `INTERRUPT` / `INTERRUPTED` wording. A later failed live
smoke for `gemini-3.8-live` is a reason to pick another row in the matrix, not
a reason to hardcode a different id.
