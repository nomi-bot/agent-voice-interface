# Component boundaries
Status: Proposed implementation decomposition.

- **Client SDKs:** audio capture/playback, AVIP negotiation, buffering and callbacks. The M0 browser adapter is `clients/browser/avip-client.js`. The M0 headless implementation is the `avip_client` package. Capture, playback, and a TypeScript build are later work.
- **Transport port:** connection setup, framed media, ordered control, event delivery and backpressure. WebSocket first.
- **Gateway core:** session manager, correlation IDs, capability registry, event routing, media routing, agent run manager and telemetry. VoiceSession, VoiceResponse and AgentRun transitions are pure machines in `avi.core.lifecycle`. They do not open sockets.
- **VoiceProvider port:** connect/session, inbound audio, outbound media/events, interruptions and errors. First provider candidate: Gemini Live (verify SDK/API contract).
- **AgentAdapter port:** submit, status, result, optional cancel and progress. First real adapter: Hermes after source/API verification.
- **DeviceAction port:** typed action request/status only. Robot-side bridge validates allowlist, operator permissions and safety before dispatch; no direct motor control.
- **ROS 2 bridge:** separate optional process/package mapping ROS topics (audio/status) and a mocked, bounded action lifecycle. Physical actuation excluded.

Interfaces and module names are provisional pending M0 contract tests. See ADRs for accepted boundaries.
