# JEV-28 · Authenticated durable runs, recovery and replay

[Linear issue](https://linear.app/jevenzzh/issue/JEV-28) · [Six M2 exit gates](../milestones.md#m2-exit-gates). Split implementation into independent store/replay, principal/authorization, and crash/cutover test modules on one issue branch. The store owner owns schema/migrations. Integrate verified remote behavior after JEV-27.

The [proposed AVIP extension](../../protocols/avip-durable-runs-v1.md) defines the
wire handoff and links the synthetic protocol vectors.

**Storage contract.** Use a transactional local store for the loopback v0.1 deployment (SQLite is the proposed default). Persist owner, run ID, request ID and canonical payload digest, remote handle, dispatch/reconciliation state, lifecycle state, timestamps, expiry and latest cursor. Unique (owner, request ID) serializes concurrent submissions. Append events and update snapshots atomically. Persist intent before contacting Hermes. Keep uncertain dispatch separate from failed execution. A crash between remote acceptance and handle persistence must not produce a second task.

**Identity and access.** Specify a loopback test credential issuer/trusted verifier, expiration and revocation, principal derivation and permission checks on submit, status, result, progress, attach and cancel. Bind the verified principal to the connection. Ignore any client-supplied owner. Authentication must work for both browser and headless clients without credentials in URLs, logs or reports. Missing or invalid credentials deny durable operations; existing media-only sessions remain usable.

**Recovery and retention.** On restart scan unfinished intents, look up provider evidence by stored correlation or handle and append each newly observed transition once. Record unresolved cases for operator reconciliation. Reattach starts after the last acknowledged cursor and returns ordered events; deduplicate by run and cursor. If history is pruned, return a snapshot and resume cursor atomically. Define configurable execution TTL and terminal result retention with documented defaults and deterministic fake-clock tests. Expiry stops new work, invokes verified cancellation if available and records outstanding remote uncertainty before cleanup. Keep sufficient tombstone/deduplication history for the documented retry window.

**Cutover.** Gate durable submissions behind an explicit switch. Stop legacy admissions, drain or explicitly cancel each existing run under its old ownership model, record all outcomes, then enable durable admissions. An unconfirmed legacy outcome blocks the switch. Rollback disables new durable admissions and keeps owner-authorized recovery and result retrieval available for existing durable work.

**Acceptance matrix.** Exercise simultaneous identical and conflicting submits; cross-owner, guessed ID, expired token and revoked access on every protected operation; crash before send, after acceptance and during result delivery; disconnect during a 60 s+ run; repeated attach, expired cursor, concurrent replay/live event, TTL and cleanup. Verify one remote execution when reconciliation is supported, otherwise a visible uncertain outcome. Demonstrate legacy cutover with pending and running work and controlled rollback. Publish actual recovery commands, persisted redacted state before/after, and all six gate outcomes. JEV-29 consumes these operations only after authorization and migration are active.

## Offline cutover and recovery rehearsal

Run from the repository root with Python 3.12+ and the development dependencies installed.

```sh
PYTHONPATH=src:tests python -m unittest -v test_jev28_admission_gates test_jev28_durable_runs test_jev28_legacy_cutover
```

| M2 gate | Evidence from the offline suite | Additional qualification |
|---|---|---|
| Hermes integration | The recovery suite uses a deterministic backend. Separate adapter probes and tests exercise the Hermes contract. | A controlled live task must return the correct result to speech. |
| Ownership and retention | Fake time advances through execution TTL, result retention and the retry window. | Check configured deployment values. |
| Authorization | Tests check cross-owner access, missing grants, revocation during operations and expired credentials. | Exercise the configured gateway issuer. |
| Recovery | A lost receipt, a process exit after the dispatch claim, changed provider scope and a lookup miss exercise separate recovery paths. | Hermes cannot resolve an accepted submission without its handle by correlation. |
| Reattach | After 65 seconds of simulated time a fresh authenticated connection reads retained progress and completion in cursor order. | Confirm client delivery and cursor handling on the deployed build. |
| Legacy cutover | The restart test forcibly exits child processes mid-submit and after acceptance. A second process reopens SQLite, observes outstanding work and refuses cutover. Confirming the known handle's terminal state under the original session scope releases it. | Drain any work predating admission tracking in its original gateway. |

Schema v2 records a local ticket before each tracked legacy dispatch, binds the returned
handle when available, and persists the admission freeze on `begin()`. A freeze survives
rollback and restart. To inspect an interrupted rehearsal, call `store.unresolved_legacy()`
from trusted local code. Each row contains the local ticket, legacy session ID and optional
run ID. There are no task arguments or results in these rows. For a known handle, provide
the original adapter and scope to `await cutover.reconcile_known_run(session_id, run_id, adapter)`.
Then call `await cutover.enable_durable()` once the outstanding set is empty. A missing
receipt without independently verifiable evidence stays blocked. Do not re-submit it.

The v1-to-v2 migration preserves existing runs and cutover receipts. V1 did not store open
legacy admissions or receipt-free freezes. Drain the old gateway before upgrading and reapply
its freeze before traffic resumes. A pre-existing cutover receipt restores the frozen state.

### Pinned Hermes through the public AVIP gateway

The optional `Hermes loopback qualification` workflow runs on manual dispatch or on
a pull request carrying the `hermes-loopback` label. It uses two gateway processes, an
actual WebSocket and a deterministic, tool-free local model. It submits a task twice,
disconnects, restarts AVI in retrieval-only mode and checks the final result and ordered
cursor replay after fresh authentication. The job records the candidate SHA, pinned
Hermes revision, execution count and outcome in the `hermes-avip-evidence` artifact.

To reproduce locally, prepare the locked Hermes checkout and Python from ADR-012 and
an AVI environment with `.[dev,hermes]` installed. Use a host with an available TCP port
8000 and POSIX Unix-domain sockets for the Hermes bootstrap control listener.

```sh
PYTHONPATH=src "$HERMES_CHECKOUT/.venv/bin/python" docs/roadmap/delivery/evidence/probe_hermes_avip.py \
  --checkout "$HERMES_CHECKOUT" --avi-python "$AVI_VENV/bin/python"
```
