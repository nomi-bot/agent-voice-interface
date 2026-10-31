# ADR-002: Gateway topology
Status: accepted for the planned MVP.

Start with Browser → AVI Gateway → Gemini Live. Keep API keys server-side.
An alternative is Browser → Gemini using ephemeral credentials, with AVI handling
agent delegation and session coordination. Benchmark latency and security tradeoffs
before changing topology. Current skeleton implements only a JSON control socket.

## Related decision

This ADR remains accepted. [008](008-transport-abstraction.md): Preserves gateway topology and server-side keys while adding a transport port.
