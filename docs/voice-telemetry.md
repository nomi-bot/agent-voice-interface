# Voice telemetry

Local measurements for one voice session. The report is JSON for a dashboard
or a CI artifact. It is not an AVIP frame and it does not add a control
message. The mock mirror (`AVIP_MOCK_MEDIA=1`) is the path this measures.
Gemini Live is not implemented here.

## Clocks

Every report names three clocks. They are not interchangeable.

| Clock | Report field | What it is | Use it for |
| --- | --- | --- | --- |
| Session | `clocks.session` | Monotonic milliseconds on one page or one process. The browser hook calls this `local`. In the page it is `performance.now` (milliseconds since the page time origin). In the gateway and the headless harness it is `time.monotonic` converted to milliseconds. | Differences between marks in **one** report. |
| Wall | `clocks.wall` | Unix-epoch milliseconds, captured once when telemetry is constructed, paired with the session clock at that instant (`anchorSessionMs`, `anchorWallMs`). | Placing the report on a timeline. |
| Media | `clocks.media` | AVIP `timestamp_us`. The sender's clock, starting at 0 in each direction. The mock mirror's downlink timestamp is the gateway media clock, not the client's. | Frame timing on that sender only. |

Do not subtract the session clock from another host, from the wall anchor, or
from a media timestamp. Do not subtract wall clocks across hosts. Do not
compute latency from the wall clock. A gateway report and a browser report
are two session clocks; their raw marks are not a cross-host latency.

`path: "mock-mirror"` end-to-end samples pair the Nth uplink send with the
Nth downlink observation on **that client's session clock**. The interval
includes the mock mirror's loopback. It is not a model latency. `cost` stays
null on this path.

## Hooks

`BrowserVoice` still emits the JEV-24 names, plus `session-start`, through
`onMetric(name, {at, clock: "local"})`. `at` is the session clock. The first
sample of each name sticks.

| Name | When |
| --- | --- |
| `capture-start` | The microphone track is live, before the socket handshake. |
| `session-start` | Capture, handshake, and the speaker are up. The session is live. |
| `first-uplink` | The first uplink frame was accepted by `send`. |
| `first-downlink` | The first downlink payload was observed. |
| `playback-start` | The first downlink frame was accepted by the speaker. This is the first audible sample. A local `stopPlayback` does not emit it. |

`intervalsMs` is the difference of those marks. `e2e.uplinkToDownlink` is the
distribution of paired samples (count, min, max, mean, nearest-rank p50 and
p95, and a histogram). Percentiles use every sample until 4096, then a
reservoir; the histogram, count, min, max, and mean stay exact.
`e2e.uplinkToAudible` is the same pairing for frames the speaker accepted.

Counters include `captureDrops`, `transmitDrops`, and `speakerDrops` (the
speaker-buffer drops already counted as `playbackDrops` on the page).
`timeouts` records `send`, `idle`, and `uplink-stalled` as short tokens.
`buffer.depthFrames` is the speaker horizon in nominal 20 ms frames on the
**audio** clock (`AudioContext.currentTime` in the page), not the session
clock. `buffer.jitter` is the absolute gap between downlink arrivals and
20 ms, on the session clock.

## Commands

Unit suite (no secrets, no soak):

```sh
python3.12 -m unittest discover -s tests -v
node clients/browser/voice-check.mjs
```

In-process soak. No microphone, no socket, no provider. Two seconds is 100
nominal frames. The speaker clock does not advance, so frame 9 and later are
speaker drops. One 9-frame capture burst and three uplink-budget overflows
are included so the drop counters move. The cap is 1800 seconds. This command
is local; CI does not run it.

```sh
node clients/browser/voice-soak.mjs --seconds 2
python -m avi.telemetry --seconds 2
```

A 30-minute local run, still in-process:

```sh
python -m avi.telemetry --seconds 1800
```

Live mock mirror. Start the gateway first. The probe does not read API keys.
The URL must not contain user info.

```sh
AVIP_MOCK_MEDIA=1 python3.12 -m uvicorn avi.main:app --host 127.0.0.1 --port 8000 --ws-max-size 16384 --ws-max-queue 16
python -m avi.telemetry --frames 20 --live ws://127.0.0.1:8000/ws/avip
node clients/browser/voice-soak.mjs --frames 20 --live ws://127.0.0.1:8000/ws/avip
```

The headless live probe has no speaker, so `playback-start` is absent and
`speakerDrops` stays 0. The Node live probe uses the real `BrowserVoice` path
with a frozen speaker clock, and paces on each downlink so the 8-message
receive cap is not overrun. The page at `http://127.0.0.1:8000/voice` shows
the same report while a real microphone is open. Use headphones.

## Interface

No AVIP field, frame layout, or close code changed.

- `onMetric` gained `session-start`. `clock` is still `"local"`.
- `voice.stats` is unchanged. `speakerDrops` in the report is `playbackDrops`.
- `GET /voice/telemetry.js` is the browser module. `voice.js` imports it.
- `voice.telemetry.report()` and `python -m avi.telemetry` print schema
  `avi.voice-telemetry/1`.
- The gateway keeps the same object on the session and logs one
  `voice_telemetry` JSON line when the socket ends. That line is not sent to
  the client. It includes the opaque session id and no PCM.

`cost` accepts only `provider`, `currency`, `inputTokens`, `outputTokens`,
`audioInputSeconds`, `audioOutputSeconds`, and `amount`. On the mock path
those values stay null. `note_cost` refuses a credential-shaped key or value
and leaves the previous cost fields unchanged. `report()` also redacts
strings that look like secrets.

The development gateway constructs telemetry with `path="mock-mirror"` and
`scenario="gateway"`. Those are constructor arguments (`telemetry_path`,
`telemetry_scenario`), not a permanent label for every future session.
JEV-23 must pass `telemetry_path="provider"` when a real provider is
attached. Leaving the default would report model latency as mock-mirror.

## Gemini

Real Gemini sessions are not part of this slice. `GeminiLiveProvider` still
raises `NotImplementedError`. The gated test uses the same operator contract
as [ADR-011](adr/011-gemini-live-compatibility.md): `GEMINI_API_KEY` is the
only credential, and `GEMINI_LIVE_MODEL` is the only model pin. `GOOGLE_API_KEY`
is ignored. There is no `AVI_GEMINI_MODEL` alias.

`tests/test_gemini_voice_integration.py` is discovered by the unit suite.
Unset `GEMINI_API_KEY` skips, including when only `GOOGLE_API_KEY` is present.
A key without `GEMINI_LIVE_MODEL` fails closed instead of choosing a model.
Both set, while the provider is still unimplemented, skips without placing a
call. Skip and failure text name the variables and do not include their
values. CI sets neither variable. The test does not print the environment and
does not put a key in a report.
