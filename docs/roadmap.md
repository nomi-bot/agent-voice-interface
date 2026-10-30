# Next implementation milestones

1. Verify current Google GenAI SDK, Live model availability and documented audio
   formats, session expiry, interruption and tool semantics against official docs.
   Pin a tested compatible SDK range. No model identifier is selected yet.
2. Build the smallest browser → gateway → Gemini PCM audio path, keeping the key
   server-side. Use bounded queues, explicit audio rates and immediate playback flush.
3. Test overlapping input/output, barge-in, disconnect/reconnect, errors and timeouts
   using actual hardware and credentials; record evidence and limitations.
4. Wire provider tool events to the deterministic mock adapter while audio streams.
   Validate arguments and preserve distinct speech / response / task cancellation.
5. Inspect Hermes source or official documentation and record a verified integration
   contract, permissions, status and cancellation support before implementing it.
6. Add monotonic latency metrics, client buffering/underrun telemetry and per-session
   resource experiments. Benchmark topology before moving the audio data plane.

The full engineering v0.1 MVP is not complete with this initialization PR.
