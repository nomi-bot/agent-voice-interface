# Legacy roadmap

**Superseded by [roadmap/milestones.md](roadmap/milestones.md)**, the canonical
AVI v0.1 M0–M4 plan. This page is a historical mapping, not a second backlog.

The original initialization milestones map to that plan as follows:

1. Verify Google GenAI SDK, Live model availability, audio/session/interruption/tool
   semantics and pin the tested SDK range → **M1**, after **M0** defines AVI's ports
   and AVIP proposal.
2. Build browser → gateway → Gemini PCM audio with server-side keys and bounded
   buffering → **M1**.
3. Validate overlapping audio, barge-in, reconnect, errors and timeouts on real
   hardware/credentials → **M1** baseline evidence and **M3** robustness gates.
4. Route provider tool events to the mock adapter, validate arguments and separate
   playback/response/task cancellation → **M0** contracts, **M2** real delegation
   and **M3** interruption tests.
5. Verify Hermes contracts, permissions, status and cancellation before integration
   → **M2**, including its ownership, persistence and migration exit gates.
6. Instrument monotonic latency, buffering/underruns and resource experiments;
   benchmark before changing topology → **M1** instrumentation, **M3** measurements
   and **M4** stability/release evidence.

**M0** adds the device-neutral foundation before provider integration. **M4** adds
the Python SDK, simulated robot and optional ROS 2 mock-action bridge; physical
actuation is excluded. See the canonical plan for scope, estimates and exit gates.
The initialization skeleton does not complete the engineering v0.1 MVP.
