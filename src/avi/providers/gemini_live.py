"""Reserved Gemini Live adapter.

The compatibility spike lives in ``avi.providers.gemini_live_spike`` and
ADR-011. This class is still not a voice provider.
"""


class GeminiLiveProvider:
    def __init__(self):
        raise NotImplementedError(
            "Gemini Live is not integrated. See docs/adr/011-gemini-live-compatibility.md"
        )
