# JEV-30 · Interruption, continuity and recovery

[Linear issue](https://linear.app/jevenzzh/issue/JEV-30). Build the fake clock/speaker, network-fault cases and browser interaction tests immediately. Consume #13's generation semantics and the durable attach/result contract when available. Own browser voice lifecycle and fault tests; coordinate any provider or gateway fix with its owner.

| User or transport event | Observable outcome |
|---|---|
| Local stop playback | Flush scheduled sound locally; microphone and task continue |
| User talks over an answer | Stop stale-generation playback, accept new uplink, play subsequent answer; backend run ID and state remain valid |
| Explicit response cancel | Cancel current provider output where supported; idle/repeated cancel leaves the next generation playable |
| Explicit task cancel | Recheck principal and capability; only verified terminal cancellation updates AgentRun |
| Socket drop, slow speaker or provider GoAway | Bound queues and close resources; after a fresh authenticated session, reattach by cursor and retrieve the retained result |

**Fault suite.** Inject late interrupted events, silent gaps, sender stalls, echo-like playback fed into capture, overlapping speech, timeout during result delivery, reconnect with expired credentials and repeated attach. Use sequence/cursor assertions and a virtual clock for ordering. Ensure bounded uplink/downlink memory, no stale output after a generation flush, no duplicate submission and no leaked mic tracks/tasks. Test a headset baseline and document observed echo behavior on a speaker setup.

**Measurement.** Timestamp detected user speech and actual playback cessation on the same client clock. Label scheduled/mocked silence separately from measured audible silence. Run a paced multi-minute session, report distribution, count, drop rates and failures. The roadmap p95 target is 300 ms, provisional pending empirical measurement. Show live speech interruption during a real Hermes task, query progress, deliver the eventual result, then demonstrate authorized explicit cancellation on a separate task. Report any skipped real-system case as pending.
