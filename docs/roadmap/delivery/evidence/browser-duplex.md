# JEV-30 browser fault evidence

The [JEV-30 acceptance spec](../30-duplex.md) covers voice continuity,
resource cleanup and authorized run replay. The browser suite drives the
production voice lifecycle and AVIP client against scripted WebSocket and Web
Audio interfaces. It uses the existing AVIP audio framing and counts sequence
numbers and retained run cursors as separate clocks.

## Reproduce

```sh
node --test clients/browser/duplex-check.mjs
node clients/browser/voice-check.mjs
python -m unittest discover -s tests -v
```

The base branch skips the generation test until it has the JEV-32 browser
handler. Run that test against a checkout containing the handler and consume
the JEV-28 golden replay fixture directly when reviewing dependencies.

```sh
AVI_DUPLEX_GENERATION_CHECKOUT="$JEV32_CHECKOUT" \
AVI_DUPLEX_DURABLE_FIXTURE="$JEV28_CHECKOUT/tests/fixtures/jev28-durable-runs-v1.json" \
  node --test clients/browser/duplex-check.mjs
```

The dependency run used [PR #13 at `bee5d33`](https://github.com/nomi-bot/agent-voice-interface/commit/bee5d3318d93689a867f03b52e992584446b17f5)
and the JEV-28 fixture in `dd5b285` with blob `98f96843cae67a022127cafd230c1b210b3f0ed6`.
All 13 tests passed with both inputs. Applying the JEV-30 browser patch to a
local copy of #13 also passed its 32 existing browser checks, including paced
20 ms audio after silence.

## Observations

| Scenario | Observed behavior |
| --- | --- |
| Stop during permission or audio initialization | Late resources close and the voice session stays stopped. The socket closes immediately even when output cleanup is still pending. |
| Sender stall after local stop | Playback stops while capture remains live. Two blocked frames advance the media timestamp by 40 ms and the next successful sequence advances by one. |
| Slow speaker and receive overflow | A frozen audio clock bounds scheduled sources to eight queued plus the active source. Elapsed sources release their budget even when end notifications are late. A receive overflow closes capture and records one queue failure. |
| Interruption during echo-like capture | The injected speech onset and scheduled stop share the virtual clock. The active source stops at 8 ms in one scripted sample. New capture and the next answer remain live after stale and repeated interruption events. |
| Three-minute virtual session | 9,000 capture frames, 300 capture-only frames during simulated response gaps and 8,700 scheduled output frames. Scheduled delays were 8 ms at p50 and p95. The peak was two scheduled sources, with zero drops and zero session failures. |
| GoAway and result-send timeout | GoAway releases tracks and output. A scripted send timeout rejects the stale result waiter. A fresh authenticated connection resumes after cursor 5 and receives the retained result at cursor 6. Repeating attach returns cursor 6 without another result event or submission. An expired grant gets `access_denied`. |
| Explicit task cancellation | The scripted peer rejects requests without the capability or authority. Its acceptance reply leaves the task running and the following status query supplies the terminal `cancelled` observation. |

The simulated timing describes scheduling on a virtual clock. Physical headset
and speaker checks, audible p95 interruption, real Gemini/Hermes continuation
and recovery, and verified live response and task cancellation remain release
checks. Use the same client clock for speech onset and audible cessation,
record the audio setup and report sample count, distribution, drop rates and
failures. Real browser recovery requires the negotiated JEV-28/29 operations;
the scripted scenarios exercise the browser transport before that wiring.
