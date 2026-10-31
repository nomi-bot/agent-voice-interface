# AVI architecture overview
Status: **Accepted direction**, 2026-10-30. Implementation is incremental; see [existing implementation notes](../architecture.md).

AVI is a device-neutral, agent-runtime-neutral conversational voice interface, **not** an agent framework or robot motion controller.

```text
Browser (TS SDK) / headless robot simulator (Python SDK) / future mobile
                |
          AVIP v1 contract
                |
   Transport adapter (WebSocket first)
                |
AVI gateway: sessions | media router | events | capability registry | runs
           /                            \
 Gemini Live voice port          Hermes agent port
                |
   Robot-specific ROS 2 bridge stays on the *client* side
```

## Invariants
- Browser and headless clients must work without device-type branching in AVI core.
- Keep media, reliable control, and asynchronous events conceptually distinct. First transport: WebSocket; future WebRTC is not an MVP dependency.
- Separate VoiceSession, VoiceResponse and AgentRun lifecycles. Playback stop never implies agent cancellation.
- Capabilities are negotiated; no model-generated text is treated as an executable motor command.
- Device-side robotics runtime authorizes and validates all actions, with independent stop/safety controls.
- Prefer an optional ROS 2 bridge so gateway and common Python SDK need no ROS 2 installation.
- Instrument latency, interruption, queue depth, errors and resources; do not claim performance targets before measurements.

## Present implementation versus desired behavior
Current initialization skeleton includes FastAPI JSON WebSocket control, the `/ws/avip` session handshake, mock PCM framing on that same socket, a mock agent, bounded queues, and session-owned runs cancelled at disconnect on `/ws`. Browser-shaped and headless clients share `/ws/avip`. The skeleton does **not** implement a live voice provider, Gemini Live, Hermes, the remaining AVIP control events, or durable AgentRuns. Moving runs beyond voice-session lifetimes is a planned M2 change, not a current capability.

## Documentation map
- [Current architecture](../architecture.md): executable skeleton behavior.
- This overview: accepted target direction, not an implementation claim.
- [Canonical milestones](../roadmap/milestones.md): single M0–M4 implementation plan.
- [ADR index and numbering policy](../adr/README.md): unique three-digit identifiers;
  001–005 remain accepted and 006–010 record extensions and decision lineage.

## References
- [Canonical milestones](../roadmap/milestones.md)
- [Protocol outline](../protocols/avip-v1.md)
- [ADRs](../adr/README.md)
