# JEV-32 · Gemini Live gateway and M1 acceptance

[Linear issue](https://linear.app/jevenzzh/issue/JEV-32) · [Existing PR](https://github.com/nomi-bot/agent-voice-interface/pull/13). Papi owns the candidate. Nomi owns the post-merge browser measurement. Review the latest head before relying on earlier credentialed evidence.

**User flow.** Open /voice on a supported loopback origin, grant microphone access and exchange two spoken turns through /ws/avip. Interrupt the first answer. Its queued audio clears and the next answer plays. Closing the page releases mic, socket, pending frames and provider session.

**Configuration contract.** Key and valid model configured selects Live even when mock is enabled. With Live unavailable, honor explicit mock configuration, otherwise disable voice. Invalid or absent model must never trigger a provider call. /health distinguishes configured path from actual successful conversation. Preserve legacy /ws. Downlink is resampled to the existing 16 kHz profile, framed at nominal 20 ms. Keep the existing bounds on scheduled playback, queued, pending and in-flight audio. GoAway closes the session with the documented provider-unavailable behavior.

**Review evidence.** Re-run the silent-gap regression after a 10 s gap and inspect the actual speaker scheduling. Confirm the 3 s reply plays over time and the lead stays within 160 ms. Stall the sender, push bursts and then an interrupt, assert aggregate byte bounds and prompt control processing. Repeat idle cancel and late generation interrupt. Capture offline suite and CI at the current head. Record which credentialed checks ran on that same head versus earlier commits, including silence, speech and interruption recovery. Keep GoAway renewal outside this PR.

**Post-merge acceptance.** Run a real microphone and speaker on the merged SHA. Record audio configuration, model, turn count, end-of-turn to first audible output, interruption to silence, drops and recovery. Capture mock and real evidence separately. Update stale roadmap and README claims only to the level observed. M1 browser acceptance remains open if only simulated evidence exists.
