# ADR-001: Python control plane
Status: accepted.

Use Python 3.12+, asyncio and FastAPI for I/O-bound gateway coordination. Keep
orchestration independent from the audio data plane so transport can later move
to Go or Rust if measurements justify it. Never run blocking agent work inline.

## Related decision

This ADR remains accepted. [006](006-hexagonal-architecture.md): Extends control-plane boundaries without changing Python/FastAPI.
