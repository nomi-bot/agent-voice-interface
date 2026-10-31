# Component boundaries
Status: Proposed implementation decomposition.

- **Client SDKs:** audio capture/playback, AVIP negotiation, buffering and callbacks. TypeScript browser and Python headless implementations.
- **Transport port:** connection setup, framed media, ordered control, event delivery and backpressure. WebSocket first.
- **Gateway core:** session manager, correlation IDs, capability registry, event routing, media routing, agent run manager and telemetry.
- **VoiceProvider port:** connect/session, inbound audio, outbound media/events, interruptions and errors. First provider candidate: Gemini Live (verify SDK/API contract).
- **AgentAdapter port:** submit, status, result, optional cancel and progress. First real adapter: Hermes after source/API verification.
- **DeviceAction port:** typed action request/status only. Robot-side bridge validates allowlist, operator permissions and safety before dispatch; no direct motor control.
- **ROS 2 bridge:** separate optional process/package mapping ROS topics (audio/status) and a mocked, bounded action lifecycle. Physical actuation excluded.

Interfaces and module names are provisional pending M0 contract tests. See ADRs for accepted boundaries.
