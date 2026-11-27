# AVI v0.1 parallel delivery plan

Planning snapshot, 2026-11-23. Base `main@f599095`; live gateway candidate [#13](https://github.com/nomi-bot/agent-voice-interface/pull/13) at `bee5d33`. These are implementation specifications. The [approved roadmap](../milestones.md), [AVIP protocol](../../protocols/avip-v1.md) and accepted [ADRs](../../adr/README.md) govern compatibility. Track completion against observable exit gates. Linear's M1 percentage excludes JEV-32.

See the [implementation review and remaining acceptance](offline-acceptance.md) for development priorities and integration status as of 2026-11-28.

## Dispatch board

One row is one independently reviewable delivery. Each Linear issue gets one integration PR. Multiple agents on one issue can contribute path-scoped commits to its topic branch through its integration owner. Role names describe work, not new assignments. Preserve Papi's JEV-32 and JEV-26 ownership and Nomi's post-merge browser soak. They need not personally execute every test. Jeven authorizes merges.

| Work item and detail | Can start immediately | Dependency before integration or sign-off | Exclusive edit area / proposed branch |
|---|---|---|---|
| [JEV-32 · Live gateway](32-live-gateway.md) | Re-review existing candidate on its own branch | Current-head review, then browser soak on merged SHA | Existing #13, gateway media path |
| [JEV-26 · Hermes contract](26-hermes-contract.md) | Pin upstream, run independent probes | Verified evidence before binding a real adapter | ADR and probe fixtures, `spec/jev-26-hermes-contract` |
| [JEV-27 · Hermes adapter](27-hermes-adapter.md) | Typed adapter tests against a fake transport | JEV-26 selects transport and permissions | Hermes adapter and its tests, `feat/jev-27-hermes` |
| [JEV-28 · Durable runs](28-durable-runs.md) | Store/replay, owner checks and crash harness in parallel | JEV-27 for reconciliation and real cutover rehearsal | Durable run service and legacy migration, `feat/jev-28-runs` |
| [JEV-29 · Voice delegation](29-voice-delegation.md) | Script provider events and mock run outcomes | JEV-32, 27, 28 for real authenticated delegation | Tool routing and final gateway composition, `feat/jev-29-delegation` |
| [JEV-30 · Interruption and recovery](30-duplex.md) | Browser scenarios and fault injection on fake sessions | JEV-32 for live playback, JEV-28/29 for task continuity | Browser lifecycle and resilience suite, `feat/jev-30-duplex` |
| [JEV-31 · SDK, simulator and ROS 2](31-robot.md) | Implement simulated client on released SDK; build bridge against fake bus | JEV-29/30 for full parity, ROS 2 runtime for bridge evidence | Client/simulator and optional bridge, `feat/jev-31-simulator` |
| [M4 release qualification](release.md) | Prepare runner, measurement schema and install checks | All delivery rows for v0.1 sign-off | Release harness and report, `test/m4-qualification` (roadmap work, no new ticket) |

## Integration order

1. Start all rows' independent work immediately. Start JEV-29 and JEV-30 scenario design with deterministic fixtures. Feature enablement stays local and opt-in. #13 owns live gateway wiring until its review completes.
2. Land the Hermes evidence and internal service contracts. Bind the verified Hermes implementation, then integrate the durable run service against it. Run the six M2 migration gates and end-to-end delegation together before claiming M2. The M1 live mount must land before enabling M2 in the gateway.
3. Exercise a running task through interruption/reconnect. Qualify the same behavior with a second client, run the ROS 2 simulation and collect release evidence. Mock client parity and local simulator tests can land earlier.

## Coordination and evidence

Use the [shared interface sheet](interfaces.md) for common vocabulary and fixture ownership. Attach the consumed contract revision and example fixture to dependent PRs. If a fixture changes, its owner updates both producer and consumer assertions before wiring. Separate Python and JS serializers must agree independently. Keep new unit suites under distinct issue-prefixed filenames. Put common gateway registration in JEV-29 after JEV-32 lands; JEV-28 owns the legacy cutover hook. Each integration owner resolves shared-file edits against the latest integration base. Branch new work from dev and integrate there for shared testing. Existing #13 retains its main base and review history. After an authorized merge to main, bring that exact merged change into dev before dependent integration. Promotion to main is a separate decision. Do not cherry-pick unreviewed gateway behavior into unrelated branches.

Every PR links its Linear issue if applicable, lists affected interfaces, records exact head, test commands, passed/failed/skipped cases, compatibility/security impacts and remaining real-system checks. Use the repo baseline `python -m unittest discover -s tests -v` and `node clients/browser/voice-check.mjs` with Python 3.12+ and the applicable extras. A fake, credentials-gated skip, credentialed API run and real microphone run are four different evidence classes. Keep proof reproducible and redact secrets, raw speech and user task contents.
