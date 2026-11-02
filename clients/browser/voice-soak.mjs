// In-process mock soak for the browser voice telemetry report.
// No microphone, no Gemini, no API key. The speaker clock stays at 0 so the
// 8-frame cap drops, matching `python -m avi.telemetry`.
//
//   node clients/browser/voice-soak.mjs --seconds 2
//   node clients/browser/voice-soak.mjs --seconds 2 --live ws://127.0.0.1:8000/ws/avip
//
// --live needs a gateway already running with AVIP_MOCK_MEDIA=1. Frames are
// paced on downlink so the browser receive cap is not overrun. The soak is
// not part of the default unit suite.

import {AvipBrowserClient} from "./avip-client.js";
import {VoiceTelemetry} from "./telemetry.js";
import {
  BrowserVoice,
  MAX_UPLINK_QUEUED_BYTES,
  SpeakerBuffer,
  trimCaptureBurst,
  uplinkQueueDecision,
} from "./voice.js";

const MAX_SECONDS = 1800;
const MAX_FRAMES = MAX_SECONDS * 50;
const SECRET = /api[_-]?key|secret|bearer\s|AIza[0-9A-Za-z_-]{8,}|sk-[A-Za-z0-9]{8,}/i;

export function framesForSeconds(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0 || seconds > MAX_SECONDS) {
    throw new Error("seconds must be a positive finite number at most 1800");
  }
  return Math.max(1, Math.round(seconds * 50));
}

export function mockSoakReport(frames) {
  if (!Number.isInteger(frames) || frames < 1 || frames > MAX_FRAMES) {
    throw new Error("frames must be an integer from 1 to 90000");
  }
  const clock = {t: 0};
  const telemetry = new VoiceTelemetry({
    now: () => clock.t,
    wallNow: () => 1700000000000,
    client: "browser",
    path: "mock-mirror",
    scenario: "mock-soak",
  });
  telemetry.mark("capture-start", 0);
  telemetry.mark("session-start", 0);
  const speaker = new SpeakerBuffer({
    now: () => 0,
    play() {
      return {stop() {}};
    },
  });
  const payload = new Uint8Array(640);
  for (let index = 0; index < frames; index += 1) {
    const uplinkAt = 10 + index * 20;
    const downlinkAt = uplinkAt + 5;
    telemetry.noteUplink(uplinkAt);
    telemetry.mark("first-uplink", uplinkAt);
    telemetry.noteDownlink(downlinkAt);
    telemetry.mark("first-downlink", downlinkAt);
    if (speaker.enqueue(payload)) {
      telemetry.mark("playback-start", downlinkAt);
      telemetry.noteAudible(downlinkAt);
    }
    telemetry.noteBufferDepth(speaker.depthFrames());
  }
  const burst = trimCaptureBurst(new Array(9));
  let transmitDrops = 0;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (uplinkQueueDecision(MAX_UPLINK_QUEUED_BYTES) === "drop") transmitDrops += 1;
  }
  telemetry.noteUplinkQueued(MAX_UPLINK_QUEUED_BYTES);
  telemetry.setCounters({
    uplinkFrames: frames,
    downlinkFrames: frames,
    playbackFrames: speaker.played,
    captureDrops: burst.dropped,
    transmitDrops,
    speakerDrops: speaker.dropped,
    timeouts: 0,
    queueFull: 0,
  });
  return telemetry.report();
}

function pageLocation() {
  return {protocol: "http:", hostname: "127.0.0.1", port: "8000"};
}

function testAudio() {
  const api = {
    samples: null,
    begin() {},
    async open({onSamples}) {
      api.samples = onSamples;
      const speaker = new SpeakerBuffer({
        now: () => 0,
        play() {
          return {stop() {}};
        },
      });
      return {speaker};
    },
    async close() {},
  };
  return api;
}

function fakeMedia() {
  const track = {
    addEventListener() {},
    stop() {},
  };
  return {
    location: () => pageLocation(),
    async getUserMedia() {
      return {getAudioTracks: () => [track], getTracks: () => [track]};
    },
  };
}

async function waitFor(predicate, label) {
  const deadline = Date.now() + 5000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(label);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

export async function liveProbe(url, frames) {
  if (typeof url !== "string" || !(url.startsWith("ws://") || url.startsWith("wss://"))) {
    throw new Error("live probe URL must be ws:// or wss://");
  }
  if (url.split("://")[1].includes("@")) throw new Error("live probe URL must not include credentials");
  const audio = testAudio();
  const voice = new BrowserVoice({media: fakeMedia(), audio});
  await voice.start({
    connect: async (lease) => {
      const client = new AvipBrowserClient({
        url,
        requestId: "req-telemetry-live",
        deviceId: "browser-telemetry-001",
        capabilities: {
          audio_input: true,
          audio_output: true,
          camera: false,
          display: false,
          robot_actions: false,
        },
      });
      lease.adopt(client);
      try {
        await client.connect({Origin: "http://127.0.0.1:8000"});
        const ready = await client.start();
        return {client, ready};
      } catch (error) {
        client.close();
        throw error;
      }
    },
  });
  try {
    for (let index = 0; index < frames; index += 1) {
      const target = index + 1;
      audio.samples(new Float32Array(320), 16000);
      await waitFor(() => voice.stats.downlinkFrames >= target, "mock mirror downlink");
    }
  } finally {
    await voice.stop();
  }
  return voice.telemetry.report();
}

function parseArgs(argv) {
  let seconds = 2;
  let frames = null;
  let live = null;
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--seconds") seconds = Number(argv[++index]);
    else if (arg === "--frames") frames = Number(argv[++index]);
    else if (arg === "--live") live = argv[++index];
    else throw new Error(`unknown argument ${arg}`);
  }
  return {seconds, frames, live};
}

function redact(error) {
  const message = error && error.message ? String(error.message) : String(error);
  return SECRET.test(message) ? "[redacted]" : message;
}

async function main() {
  try {
    const args = parseArgs(process.argv.slice(2));
    const frames = args.frames === null ? framesForSeconds(args.seconds) : args.frames;
    const report = mockSoakReport(frames);
    if (!args.live) {
      process.stdout.write(JSON.stringify(report));
      process.stdout.write("\n");
      return;
    }
    const live = await liveProbe(args.live, frames);
    process.stdout.write(JSON.stringify({inProcess: report, live}));
    process.stdout.write("\n");
  } catch (error) {
    process.stderr.write(`mock mirror probe failed: ${redact(error)}\n`);
    process.exitCode = 1;
  }
}

await main();
