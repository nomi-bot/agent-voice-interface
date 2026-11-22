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

### Integration gates

- Wire the router into the reviewed Live gateway after #13 and the authorized durable service are available on `dev`. Route the model's tool-call expiry event before generic provider-error handling. Register the negotiated JEV-28 operations and keep media-only sessions on the existing path.
- Send one structured reply per Gemini call. Use a new status-query turn for the eventual result. This path works with the scripted provider and a fresh authenticated session holding the AVI run ID. Extended Gemini follow-up over an already acknowledged call remains unverified.
- A disconnected reply after admission preserves the recorded run binding. A process exit between run admission and journal binding can leave only the stable request ID. The current durable API retrieves by run ID, so recovery when the caller has no receipt needs a reviewed owner-scoped lookup or equivalent recovery contract. The router leaves the run in the durable service and issues no fresh submission for recovery.
- Keep the journal private to the server and align its retention with the durable retry window when wiring the gateway. Run a real Gemini microphone utterance through the selected Hermes deployment, including one denial and one upstream failure. Record provider reply and audible result separately.
