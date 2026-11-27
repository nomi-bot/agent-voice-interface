# JEV-29 · Spoken request to Hermes and back

[Linear issue](https://linear.app/jevenzzh/issue/JEV-29). Start a scripted provider plus fake run-service suite from the dev baseline. Final composition depends on JEV-32, JEV-27 and JEV-28. This issue owns gateway registration of the new flow and the provider's delegation-tool translation.

**Scenario.** User asks for a bounded read-only task. Gemini emits an allowlisted typed call. Router validates the tool name, argument schema, length, principal permission and approval policy. Reserve an idempotent submission with the durable service, acknowledge it according to the verified provider tool behavior, and deliver a bounded spoken result when ready. Record session, turn, response, call, request and AVI run correlations server-side. Preserve the existing adapter acknowledgment tool for its original smoke cases.

**Routing contract.** Derive a stable request ID for redelivery of the same call in its original session. Never trust model-supplied owner or upstream handle. New caller intent gets a new request ID. Persist the binding between call and run. Emit progress only when verified, coalesce repeated updates and keep audio processing responsive during long work. Return structured results through the provider port. Verify whether the chosen model supports asynchronous follow-up; if it does not, document a supported fresh-turn/status-query flow. Never reply to an expired provider call on a different session.

**Failure and recovery.** Unknown tool, invalid arguments, unauthorized owner or denied approval returns a bounded structured error with no dispatch. An ambiguous remote submit returns pending reconciliation. Persist terminal result before speech delivery. After disconnect, the authenticated user can retrieve it on a fresh session. A retry must not execute the task again. Track attempted vs acknowledged delivery and document any possible duplicate spoken result across a crash.

**Acceptance.** Deterministic fixtures check happy path, repeated call, denial, timeout, interrupted voice, result after disconnect and two users with simultaneous calls. A credentialed test must trace one spoken request through real Gemini and real Hermes and back to audible speech; include one denied task and one upstream failure. Report run count, correlation and observed terminal state without raw inputs or secret handles. Unblock JEV-30/31 with a replayable fake transcript and the versioned dispatch/result fixture.

## Router and test handoff

`VoiceToolSession` consumes the existing provider `VoiceEvent` contract. The gateway supplies the server-generated session, turn and response IDs, a trusted authenticated connection, and a trusted submission approval callback that checks the live submit grant before applying policy. The durable service rechecks grants and owns reservation, status and results. The provider declarations are opt-in via `GeminiLiveProvider(enable_delegation_tools=True)`. Compose this option only with authenticated routing and the reviewed read-only Hermes profile. The original `avi_ack` declaration remains the default.

| Tool | Arguments and reply |
| --- | --- |
| `avi_delegate_read_only` | One nonblank `query`. The canonical `read_only` task envelope fits within the durable service's 4,096-byte ASCII JSON bound. Replies carry an AVI run ID and its confirmed status. A timeout without a returned snapshot carries `pending_reconciliation` without an ID. |
| `avi_read_run_result` | One AVI `run_id`. The durable service checks the caller's result grant and ownership before reading. Completed output includes up to 1,024 characters for speech. The service retains the full result according to its result-retention policy. |

`VoiceToolSession.accept` starts bounded background work so gateway media processing can continue. Route `tool_call_cancelled` to the same session to expire the listed pending calls. Playback interruption alone leaves the calls and runs independent. `close()` detaches the provider reply path and lets already-started durable operations settle. Explicit task cancellation remains an authenticated durable-run operation.

The [replay fixture](../../../tests/fixtures/jev29-voice-delegation-v1.json) is an internal provider/router transcript. It gives JEV-30 and JEV-31 synthetic submission, uncertain admission, interruption and result examples. Use JEV-28's fixture for AVIP wire envelopes under `durable_runs`. The status-query mode below emits no synthetic progress. Owners with replay grants can consume verified progress through the run service.

The journal binds each provider call to its stable request ID before dispatch and records the AVI run ID when admission returns. It stores IDs, a payload digest and reply state. An `attempted` reply may have reached the provider even when the SDK write fails. `acknowledged` means the SDK accepted the write. Audible speech is measured separately. A repeated call in the same session never sends a second reply after an attempt. A fresh authenticated status query may speak a retained result a caller already heard if a previous session ended before they observed its outcome.

### Gateway integration and remaining acceptance

The opt-in gateway binds a distinct durable control channel to each AVIP socket. Live declares the two Hermes tools only after that client negotiates durable controls. The provider may emit calls before authentication. The journal preserves each call's original grant and rejects unauthorized calls without dispatch, including after a later authentication on the same socket. The default media-only gateway keeps the acknowledgement tool. Provider expiry detaches the expired call's reply. Playback interruption leaves admitted runs active.

A new authenticated turn can query the result by AVI run ID. The service's owner-scoped admitted-request lookup recovers missing journal bindings after a crash without submitting a task. A closed socket's private journal rows expire after the retry window. Single-process startup marks abandoned sockets closed before admitting new ones.

The ASGI integration test uses a scripted provider and a deterministic backend. It covers an allowed delegation, policy denial, late authentication, provider tool expiry, interruption, a fresh-session result and cross-owner isolation. Run it with `python -m unittest discover -s tests -p test_voice_gateway_delegation.py -v`. Live Gemini and Hermes plus the real mic/speaker acceptance still require a separately recorded run. Measure provider reply acceptance and audible result separately.

### Operator-run prerecorded speech qualification

Use two operator-supplied synthetic **mono, 16 kHz, signed 16-bit PCM WAV** files (up to 30 seconds each). In the first, ask Gemini to delegate the exact approved read-only query to the agent, verbatim. In the second, ask it to look up the retained result of the task it just submitted using its run ID and speak the result. Use a synthetic task with a recognizable answer for later listening. Keep the exact query text in a local UTF-8 file with at most a trailing newline. Speech recognition or model paraphrasing can change the submitted text and trigger the launcher's exact-match denial. Inspect the fixture or wording before any new attempt.

Install the Live and Hermes extras and run the project's pinned Hermes API server with its isolated, tool-disabled profile. Use a fresh private state directory for this test. In the gateway environment configure `HERMES_API_KEY`, `GEMINI_API_KEY` and the operator-selected `GEMINI_LIVE_MODEL`. Start the local gateway with a backend scope that identifies this origin, profile and credential epoch.

```sh
python -m avi.local_gateway --state-dir "$AVI_STATE" --hermes-origin "$HERMES_ORIGIN" \
  --backend-scope "$AVI_BACKEND_SCOPE" --owner speech-qualification \
  --allow-query "$(cat "$APPROVED_QUERY_FILE")"
```

From the repository root the following command opens one live, potentially billable voice session. It uses the launcher's private grant on numeric loopback and the two WAVs as paced audio input. It never generates a text submission or retries a voice turn. The optional `--review-wav` creates a new owner-only WAV containing downlink audio received from the start of the follow-up turn. Omit that option to discard audio. Keep this recording private.

```sh
python docs/roadmap/delivery/evidence/probe_spoken_delegation.py --live \
  --state-dir "$AVI_STATE" --query-file "$APPROVED_QUERY_FILE" \
  --command-wav "$SYNTHETIC_COMMAND_WAV" --followup-wav "$SYNTHETIC_FOLLOWUP_WAV" \
  --review-wav "$NEW_PRIVATE_REVIEW_WAV"
```

The default invocation prints a skip without connecting. Missing fixtures or local grant also skip. Once attempted, an incomplete run exits nonzero, including the 150-second deadline. A timeout after submission can leave a running task. Inspect the owner’s recent runs before considering a separate attempt.

| Report observation | Interpretation |
| --- | --- |
| Exactly one session-bound submission with the approved task digest, accepted backend handle, completed state and retained nonempty result | The configured backend reported completion for this session's approved task. This evidence cannot establish how often the upstream model internally executed. |
| Delegation and result-read replies acknowledged | The provider SDK accepted replies correlated to this session and run. The report omits prompts, results, run IDs, provider handles, grants and model text. |
| Positive PCM bytes observed after the journal showed the result-read acknowledgement | The client received downlink PCM after that observation. A short answer that finished before the poll observed acknowledgement can yield an incomplete report. |
| Human spoken accuracy | Initially `unreviewed`. Listen to the private follow-up WAV, compare it to the expected synthetic answer, and separately record correct, incorrect or indeterminate. Keep task content and recordings out of the shared report. |

This probe covers the positive, credentialed transport path. Denied tasks, upstream failures, acoustic playback latency and physical devices have separate acceptance gates. To validate the probe offline, run `python -m unittest discover -s tests -p test_spoken_qualification.py -v`.
