# M4 · Release qualification and reproducibility

[Roadmap targets](../milestones.md). This M4 work package starts with harness design. Final qualification consumes the integrated M1–M4 paths. Use a docs/test branch and publish a report tied to the actual release candidate. A tag or external release requires a separate release decision.

| Scenario | Evidence and decision |
|---|---|
| Clean install | Install default package and optional Live extra separately on a fresh Python 3.12+ environment; start loopback gateway, load /voice, run mock and headless commands. Record OS, Python, Node, model, SDK and SHA. |
| Protocol compatibility | Run complete required handshake, media and new negotiated-control vectors in Python and JS, plus legacy /ws smoke. All required vectors must pass. |
| Simple live turns | Measure end of user turn → first audible output on a single client clock. Report count, p50, p95, errors and provisional median target ≤1.2 s. Disclose any acoustic proxy. |
| Barge-in and task delegation | Report p95 interruption-to-stop against 300 ms and successful controlled Hermes delegations / all attempted against 95%. Define success as the intended task executed once and its correct result delivered. Include raw counts, intentional negative cases separately, model and network topology. |
| 30-minute stability | Run 30 minutes through the gateway with actual socket clients. Record client mix, concurrent sessions, turns, interruptions, drops, reconnects, peak RSS, CPU, task outcomes, provider usage and cost when available. Unknown cost stays null. Require zero unrecoverable failures. Distinguish simulated socket run, real provider run and in-process soak. |
| Portability | Include browser and headless parity on the same build plus the two-way ROS mock action evidence from JEV-31. |

**Report contract.** Each row records command, SHA, dependency versions, start/end, sample count, environment, result, evidence and limitations. Record measurement clock and any skipped or unavailable path. Keep operator-supplied keys out of artifacts. Publish a reproducible local quickstart, supported capability matrix, limitations, and instructions to disable Live or durable submissions while retaining access to existing runs. Explicitly list any missed provisional target with evidence and a proposed disposition for Jeven. Only update roadmap exit status after the corresponding evidence exists.
