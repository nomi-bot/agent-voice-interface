// Node checks for the browser voice path. No microphone and no Web Audio.
// Python discovery runs this file. A failing check prints JSON and exits 1.

import {readFileSync} from "node:fs";
import assert from "node:assert/strict";

import {AUDIO_DOWNLINK, AUDIO_UPLINK, AvipBrowserClient, packFrame, unpackFrame} from "./avip-client.js";
import {FrameChunker, GEMINI_LIVE_OUTPUT_RATE_HZ, S16leResampler, floatToS16le, resampleS16leToAvip, s16leToFloat} from "./pcm.js";
import {
  BrowserVoice,
  MAX_UPLINK_QUEUED_BYTES,
  SpeakerBuffer,
  UPLINK_CLOSE_QUEUED_BYTES,
  UPLINK_FRAME_BYTES,
  developmentGatewayOrigin,
  pageOrigin,
  playbackSchedule,
  secureContext,
  uplinkQueueDecision,
} from "./voice.js";

const SILENCE = new Uint8Array(640);

function pageLocation(origin) {
  const url = new URL(origin);
  return {protocol: url.protocol, hostname: url.hostname, port: url.port};
}

function makeTrack() {
  return {
    readyState: "live",
    stopped: false,
    _ended: null,
    addEventListener(type, fn) {
      if (type === "ended") this._ended = fn;
    },
    stop() {
      this.stopped = true;
      this.readyState = "ended";
    },
    end() {
      this.readyState = "ended";
      if (this._ended) this._ended();
    },
  };
}

function fakeMedia(origin, getUserMediaImpl) {
  const created = [];
  const media = {
    created,
    calls: 0,
    location: () => pageLocation(origin),
    async getUserMedia() {
      media.calls += 1;
      if (getUserMediaImpl) return getUserMediaImpl();
      const track = makeTrack();
      created.push(track);
      return {getAudioTracks: () => [track], getTracks: () => [track]};
    },
  };
  return media;
}

function testAudio() {
  const api = {
    begun: 0,
    closed: 0,
    samples: null,
    speaker: null,
    played: [],
    stopped: 0,
    begin() {
      api.begun += 1;
    },
    async open({onSamples}) {
      api.samples = onSamples;
      api.speaker = new SpeakerBuffer({
        now: () => 0,
        play(payload) {
          api.played.push(payload);
          return {stop() { api.stopped += 1; }};
        },
      });
      return {speaker: api.speaker};
    },
    async close() {
      api.closed += 1;
    },
  };
  return api;
}

function liveClient() {
  const client = new AvipBrowserClient({
    url: "ws://127.0.0.1:8000/ws/avip",
    requestId: "req-voice-001",
    deviceId: "browser-voice-001",
    capabilities: {
      audio_input: true,
      audio_output: true,
      camera: false,
      display: false,
      robot_actions: false,
    },
  });
  client.state = "open";
  client.started = true;
  const writes = [];
  client.socket = {
    readyState: 1,
    bufferedAmount: 0,
    send(data) {
      writes.push(data);
    },
    close() {
      this.readyState = 3;
    },
    addEventListener() {},
  };
  client.writes = writes;
  return client;
}

function readyCapabilities(overrides) {
  return {
    audio_input: true,
    audio_output: true,
    camera: false,
    display: false,
    robot_actions: false,
    ...overrides,
  };
}

function boot(options = {}) {
  const client = options.client || liveClient();
  const audio = testAudio();
  const media = fakeMedia(options.origin || "http://127.0.0.1:8000/voice", options.getUserMedia);
  const statuses = [];
  const metrics = [];
  const clock = {value: 10};
  const voice = new BrowserVoice({
    now: () => clock.value,
    wallNow: options.wallNow,
    onMetric(name, detail) {
      metrics.push({name, at: detail.at, clock: detail.clock});
    },
    onStatus(name, detail) {
      statuses.push({name, detail});
    },
    media,
    audio,
    downlinkRateHz: options.downlinkRateHz,
  });
  const ctx = {voice, client, audio, media, statuses, metrics, clock, connects: 0};
  ctx.start = voice.start({
    connect: async (lease) => {
      if (options.connect) return options.connect(lease, ctx);
      ctx.connects += 1;
      if (options.beforeReady) await options.beforeReady(ctx);
      return {client, ready: {capabilities: readyCapabilities(options.capabilities)}};
    },
  });
  return ctx;
}

async function flush() {
  for (let i = 0; i < 12; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function names(statuses) {
  return statuses.map((item) => item.name);
}

function pushDownlink(client, payload = SILENCE) {
  client._push(packFrame(AUDIO_DOWNLINK, 0, 0, payload));
}

async function waitFor(predicate, label) {
  const deadline = Date.now() + 2000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(label);
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

async function runLive(url) {
  const audio = testAudio();
  const media = fakeMedia("http://127.0.0.1:8000/voice");
  const voice = new BrowserVoice({media, audio});
  await voice.start({
    connect: async () => {
      const client = new AvipBrowserClient({
        url,
        requestId: "req-voice-live",
        deviceId: "browser-voice-001",
        capabilities: {
          audio_input: true,
          audio_output: true,
          camera: false,
          display: false,
          robot_actions: false,
        },
      });
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
    const tone = new Float32Array(320);
    tone[0] = 0.5;
    const expected = floatToS16le(tone);
    audio.samples(tone, 16000);
    await waitFor(() => audio.played.length === 1, "mock mirror did not play the first frame");
    assert.deepEqual(audio.played[0], expected);
    voice.stopPlayback();
    audio.samples(new Float32Array(320), 16000);
    await waitFor(() => voice.stats.downlinkFrames === 2, "session did not stay open after local stop");
    assert.equal(voice.stats.uplinkFrames, 2);
    assert.equal(audio.played.length, 1);
    assert.equal(voice.client.state, "open");
  } finally {
    await voice.stop();
  }
  process.stdout.write(JSON.stringify({
    ok: true,
    uplink: voice.stats.uplinkFrames,
    downlink: voice.stats.downlinkFrames,
    played: audio.played.length,
    report: voice.telemetry.report(),
  }));
}

if (process.argv[2] === "live") {
  await runLive(process.argv[3]);
  process.exit(0);
}

const checks = [];

async function check(name, fn) {
  try {
    await fn();
    checks.push({name, ok: true});
  } catch (error) {
    checks.push({name, ok: false, message: error && error.stack ? error.stack : String(error)});
  }
}

await check("pcm-s16le-extremes", async () => {
  const encoded = floatToS16le(Float32Array.from([0, 1, -1, 2, -2]));
  const view = new DataView(encoded.buffer);
  assert.equal(encoded.length, 10);
  assert.equal(view.getInt16(0, true), 0);
  assert.equal(view.getInt16(2, true), 32767);
  assert.equal(view.getInt16(4, true), -32768);
  assert.equal(view.getInt16(6, true), 32767);
  assert.equal(view.getInt16(8, true), -32768);
  const decoded = s16leToFloat(encoded);
  assert.equal(decoded[0], 0);
  assert.equal(decoded[1], 1);
  assert.equal(decoded[2], -1);
  assert.throws(() => s16leToFloat(new Uint8Array([0])), /payload/);
});

await check("chunker-keeps-a-partial-frame", async () => {
  const chunker = new FrameChunker();
  assert.equal(chunker.push(new Float32Array(100).fill(0.25), 16000).length, 0);
  const frames = chunker.push(new Float32Array(220).fill(0.25), 16000);
  assert.equal(frames.length, 1);
  assert.equal(frames[0].length, 640);
  assert.deepEqual(frames[0], floatToS16le(new Float32Array(320).fill(0.25)));
  assert.equal(chunker.pending.length, 0);
});

await check("chunker-resamples-48k-across-pushes", async () => {
  const split = new FrameChunker();
  assert.equal(split.push(new Float32Array(480).fill(0.5), 48000).length, 0);
  const frames = split.push(new Float32Array(480).fill(0.5), 48000);
  assert.equal(frames.length, 1);
  assert.ok(s16leToFloat(frames[0]).every((value) => Math.abs(value - 0.5) < 0.001));
  const once = new FrameChunker();
  const whole = once.push(new Float32Array(960).fill(0.5), 48000);
  assert.equal(whole.length, 1);
  assert.equal(once.pending.length, 0);
});

await check("secure-context-and-gateway-origin", async () => {
  const secure = secureContext(pageLocation("https://example.com/voice"));
  assert.equal(secure.ok, true);
  assert.equal(developmentGatewayOrigin(pageLocation("https://example.com/voice")), false);
  assert.equal(pageOrigin(pageLocation("http://127.0.0.1:8000/voice")), "http://127.0.0.1:8000");
  assert.equal(developmentGatewayOrigin(pageLocation("http://127.0.0.1:8000/voice")), true);
  assert.equal(developmentGatewayOrigin(pageLocation("http://localhost:8000/voice")), true);
  assert.equal(secureContext(pageLocation("http://localhost:8001/voice")).ok, true);
  assert.equal(developmentGatewayOrigin(pageLocation("http://localhost:8001/voice")), false);
  assert.equal(secureContext(pageLocation("http://[::1]:8000/voice")).ok, true);
  const file = secureContext(pageLocation("file:///tmp/voice.html"));
  assert.equal(file.ok, false);
  assert.equal(file.code, "insecure_origin");
  assert.equal(secureContext(pageLocation("http://192.168.1.10:8000/voice")).ok, false);
});

await check("permission-denied-opens-no-socket", async () => {
  const denied = Object.assign(new Error("denied"), {name: "NotAllowedError"});
  const ctx = boot({getUserMedia: async () => { throw denied; }});
  try {
    await assert.rejects(ctx.start, (error) => error.code === "permission_denied");
    assert.equal(ctx.connects, 0);
    assert.equal(ctx.media.calls, 1);
    assert.equal(ctx.media.created.length, 0);
    assert.equal(ctx.audio.begun, 1);
    assert.equal(ctx.audio.closed, 1);
    assert.deepEqual(names(ctx.statuses), ["requesting-permission", "permission-denied"]);
    assert.deepEqual(ctx.metrics, []);
  } finally {
    await ctx.voice.stop();
  }
});

await check("insecure-origin-does-not-request-a-microphone", async () => {
  const ctx = boot({origin: "file:///tmp/voice.html"});
  try {
    await assert.rejects(ctx.start, (error) => error.code === "insecure_origin");
    assert.equal(ctx.media.calls, 0);
    assert.equal(ctx.connects, 0);
    assert.equal(ctx.audio.begun, 0);
    assert.equal(names(ctx.statuses)[0], "insecure-origin");
  } finally {
    await ctx.voice.stop();
  }
});

await check("security-error-is-an-insecure-origin", async () => {
  const failure = Object.assign(new Error("insecure"), {name: "SecurityError"});
  const ctx = boot({getUserMedia: async () => { throw failure; }});
  try {
    await assert.rejects(ctx.start, (error) => error.code === "insecure_origin");
    assert.equal(ctx.connects, 0);
    assert.equal(ctx.audio.closed, 1);
    assert.ok(names(ctx.statuses).includes("insecure-origin"));
  } finally {
    await ctx.voice.stop();
  }
});

await check("missing-track-releases-the-stream", async () => {
  const ctx = boot({
    getUserMedia: async () => ({getAudioTracks: () => [], getTracks: () => []}),
  });
  try {
    await assert.rejects(ctx.start, (error) => error.code === "device_ended");
    assert.equal(ctx.connects, 0);
    assert.ok(names(ctx.statuses).includes("device-ended"));
  } finally {
    await ctx.voice.stop();
  }
});

await check("uplink-uses-the-existing-avip-frame", async () => {
  const ctx = boot();
  try {
    await ctx.start;
    ctx.clock.value = 20;
    ctx.audio.samples(new Float32Array(320), 16000);
    assert.equal(ctx.client.writes.length, 1);
    assert.ok(ctx.client.writes[0] instanceof Uint8Array);
    assert.deepEqual(ctx.client.writes[0], packFrame(AUDIO_UPLINK, 0, 0, SILENCE));
    const unpacked = unpackFrame(ctx.client.writes[0]);
    assert.equal(unpacked.direction, AUDIO_UPLINK);
    assert.equal(unpacked.sequence, 0);
    assert.equal(unpacked.timestampUs, 0n);
    assert.equal(unpacked.payload.length, 640);
    assert.equal(ctx.voice.metrics["capture-start"], 10);
    assert.equal(ctx.voice.metrics["first-uplink"], 20);
    assert.equal(ctx.metrics[0].clock, "local");
    assert.equal(ctx.client.state, "open");
  } finally {
    await ctx.voice.stop();
  }
});

await check("capture-burst-keeps-eight-newest-frames", async () => {
  const ctx = boot();
  try {
    await ctx.start;
    const samples = new Float32Array(9 * 320);
    for (let frame = 0; frame < 9; frame += 1) {
      samples.fill((frame + 1) / 10, frame * 320, (frame + 1) * 320);
    }
    ctx.audio.samples(samples, 16000);
    assert.equal(ctx.client.writes.length, 8);
    assert.equal(ctx.voice.stats.captureDrops, 1);
    const first = unpackFrame(ctx.client.writes[0]);
    assert.equal(first.sequence, 0);
    assert.equal(new DataView(first.payload.buffer, first.payload.byteOffset, first.payload.byteLength).getInt16(0, true), 6553);
    const last = unpackFrame(ctx.client.writes[7]);
    assert.equal(last.sequence, 7);
  } finally {
    await ctx.voice.stop();
  }
});

await check("downlink-plays-and-records-timing", async () => {
  const ctx = boot();
  try {
    await ctx.start;
    ctx.audio.samples(new Float32Array(320), 16000);
    ctx.clock.value = 30;
    await flush();
    pushDownlink(ctx.client);
    await flush();
    assert.equal(ctx.voice.metrics["first-downlink"], 30);
    assert.equal(ctx.voice.metrics["playback-start"], 30);
    assert.equal(ctx.audio.played.length, 1);
    assert.equal(ctx.audio.played[0].length, 640);
    assert.equal(ctx.voice.stats.playbackFrames, 1);
    assert.deepEqual(ctx.metrics.map((item) => item.name), [
      "capture-start",
      "session-start",
      "first-uplink",
      "first-downlink",
      "playback-start",
    ]);
  } finally {
    await ctx.voice.stop();
  }
});

await check("stop-playback-is-local", async () => {
  const ctx = boot();
  try {
    await ctx.start;
    ctx.audio.samples(new Float32Array(320), 16000);
    await flush();
    pushDownlink(ctx.client);
    await flush();
    const writes = ctx.client.writes.length;
    ctx.voice.stopPlayback();
    assert.equal(ctx.client.writes.length, writes);
    assert.ok(ctx.client.writes.every((item) => item instanceof Uint8Array));
    assert.equal(ctx.client.state, "open");
    assert.equal(ctx.client.sequence, 1);
    assert.ok(ctx.audio.stopped >= 1);
    ctx.clock.value = 40;
    ctx.audio.samples(new Float32Array(320), 16000);
    assert.equal(ctx.client.writes.length, writes + 1);
    assert.equal(ctx.client.sequence, 2);
    assert.equal(unpackFrame(ctx.client.writes[1]).sequence, 1);
    assert.equal(unpackFrame(ctx.client.writes[1]).timestampUs, 20000n);
    pushDownlink(ctx.client);
    await flush();
    assert.equal(ctx.audio.played.length, 1);
    assert.equal(ctx.voice.stats.downlinkFrames, 2);
    assert.equal(ctx.voice.stats.playbackFrames, 1);
    assert.ok(names(ctx.statuses).includes("playback-stopped"));
    assert.equal(names(ctx.statuses).includes("disconnected"), false);
    assert.equal(names(ctx.statuses).includes("ended"), false);
    assert.equal(ctx.statuses.find((item) => item.name === "playback-stopped").detail.scope, "local");
  } finally {
    await ctx.voice.stop();
  }
});

await check("stop-playback-before-audio-does-not-start-the-speaker", async () => {
  const ctx = boot();
  try {
    await ctx.start;
    ctx.voice.stopPlayback();
    pushDownlink(ctx.client);
    await flush();
    assert.equal(ctx.audio.played.length, 0);
    assert.equal(ctx.voice.metrics["playback-start"], undefined);
    assert.equal(ctx.voice.metrics["first-downlink"], 10);
    assert.equal(ctx.client.state, "open");
    assert.equal(ctx.client.writes.length, 0);
  } finally {
    await ctx.voice.stop();
  }
});

await check("playback-buffer-drops-past-eight-frames", async () => {
  const sent = [];
  let stopped = 0;
  const buffer = new SpeakerBuffer({
    now: () => 0,
    play(payload) {
      sent.push(payload);
      return {stop() { stopped += 1; }};
    },
  });
  const payload = new Uint8Array(640);
  for (let i = 0; i < 10; i += 1) buffer.enqueue(payload);
  assert.equal(buffer.played, 8);
  assert.equal(buffer.dropped, 2);
  assert.equal(sent.length, 8);
  buffer.stop();
  assert.equal(stopped, 8);
  assert.equal(sent.length, 8);
});

await check("track-ended-stops-capture-and-leaves-the-session-open", async () => {
  const ctx = boot();
  try {
    await ctx.start;
    ctx.audio.samples(new Float32Array(320), 16000);
    const track = ctx.media.created[0];
    track.end();
    assert.equal(track.stopped, true);
    assert.equal(ctx.client.state, "open");
    assert.ok(names(ctx.statuses).includes("device-ended"));
    ctx.audio.samples(new Float32Array(320), 16000);
    assert.equal(ctx.client.writes.length, 1);
    assert.equal(names(ctx.statuses).includes("disconnected"), false);
  } finally {
    await ctx.voice.stop();
  }
});

await check("track-ended-during-connect-closes-without-uplink", async () => {
  const ctx = boot({
    beforeReady(current) {
      current.media.created[0].end();
    },
  });
  try {
    await assert.rejects(ctx.start, (error) => error.code === "device_ended");
    assert.equal(ctx.client.writes.length, 0);
    assert.equal(ctx.client.state, "closed");
    assert.equal(ctx.media.created[0].stopped, true);
  } finally {
    await ctx.voice.stop();
  }
});

await check("avip-disconnect-releases-the-microphone-and-speaker", async () => {
  const ctx = boot();
  try {
    await ctx.start;
    ctx.audio.samples(new Float32Array(320), 16000);
    await flush();
    pushDownlink(ctx.client);
    await flush();
    ctx.client._fail(new Error("socket closed"));
    await flush();
    assert.equal(ctx.client.state, "closed");
    assert.equal(ctx.media.created[0].stopped, true);
    assert.ok(ctx.audio.stopped >= 1);
    assert.equal(ctx.audio.closed, 1);
    assert.ok(names(ctx.statuses).includes("disconnected"));
    ctx.audio.samples(new Float32Array(320), 16000);
    assert.equal(ctx.client.writes.length, 1);
  } finally {
    await ctx.voice.stop();
  }
});

await check("queue-full-stays-open-and-later-audio-still-plays", async () => {
  const ctx = boot();
  try {
    await ctx.start;
    await flush();
    ctx.client._push(JSON.stringify({
      protocol: "avip/1",
      type: "error",
      code: "queue_full",
      field: "payload",
    }));
    await flush();
    assert.equal(ctx.client.state, "open");
    assert.ok(names(ctx.statuses).includes("avip-error"));
    ctx.clock.value = 25;
    pushDownlink(ctx.client);
    await flush();
    assert.equal(ctx.audio.played.length, 1);
    assert.equal(ctx.voice.metrics["playback-start"], 25);
    assert.equal(ctx.client.state, "open");
  } finally {
    await ctx.voice.stop();
  }
});

await check("audio-unavailable-releases-the-microphone", async () => {
  const ctx = boot({capabilities: {audio_input: false, audio_output: false}});
  try {
    await assert.rejects(ctx.start, (error) => error.code === "audio_unavailable");
    assert.equal(ctx.media.created[0].stopped, true);
    assert.equal(ctx.client.state, "closed");
    assert.equal(ctx.client.writes.length, 0);
    assert.equal(ctx.voice.metrics["capture-start"], 10);
    assert.equal(ctx.voice.metrics["first-uplink"], undefined);
    assert.ok(names(ctx.statuses).includes("audio-unavailable"));
  } finally {
    await ctx.voice.stop();
  }
});

await check("robot-actions-are-refused", async () => {
  const ctx = boot({capabilities: {robot_actions: true}});
  try {
    await assert.rejects(ctx.start, (error) => error.code === "invalid_state");
    assert.equal(ctx.client.state, "closed");
    assert.equal(ctx.client.writes.length, 0);
    assert.equal(ctx.media.created[0].stopped, true);
  } finally {
    await ctx.voice.stop();
  }
});

await check("uplink-queue-decision-matches-the-byte-budget", async () => {
  assert.equal(UPLINK_FRAME_BYTES, 664);
  assert.equal(uplinkQueueDecision(0), "send");
  assert.equal(uplinkQueueDecision(MAX_UPLINK_QUEUED_BYTES - UPLINK_FRAME_BYTES), "send");
  assert.equal(uplinkQueueDecision(MAX_UPLINK_QUEUED_BYTES - UPLINK_FRAME_BYTES + 1), "drop");
  assert.equal(uplinkQueueDecision(MAX_UPLINK_QUEUED_BYTES), "drop");
  assert.equal(uplinkQueueDecision(UPLINK_CLOSE_QUEUED_BYTES), "close");
  assert.equal(uplinkQueueDecision(undefined), "close");
  assert.equal(uplinkQueueDecision(-1), "close");
});

await check("capture-gap-moves-the-clock-without-a-sequence", async () => {
  const client = liveClient();
  client.noteCaptureGap(20000);
  assert.equal(client.sequence, 0);
  assert.equal(client.timestampUs, 20000n);
  client.sendUplink(new Uint8Array(640));
  assert.equal(client.sequence, 1);
  assert.equal(client.timestampUs, 40000n);
  assert.equal(unpackFrame(client.writes[0]).timestampUs, 20000n);
  assert.equal(unpackFrame(client.writes[0]).sequence, 0);
});

await check("uplink-budget-drops-on-a-non-draining-socket", async () => {
  const client = liveClient();
  client.socket.send = function send(data) {
    client.writes.push(data);
    this.bufferedAmount += data.byteLength;
  };
  const ctx = boot({client});
  try {
    await ctx.start;
    ctx.audio.samples(new Float32Array(16 * 320), 16000);
    assert.equal(ctx.voice.stats.captureDrops, 8);
    assert.equal(client.writes.length, 8);
    assert.equal(client.writes[0].byteLength, UPLINK_FRAME_BYTES);
    assert.equal(client.socket.bufferedAmount, MAX_UPLINK_QUEUED_BYTES);
    assert.equal(client.sequence, 8);
    assert.equal(ctx.voice.stats.transmitDrops, 0);
    assert.equal(ctx.voice.stats.uplinkFrames, 8);
    ctx.audio.samples(new Float32Array(320 * 3), 16000);
    assert.equal(client.writes.length, 8);
    assert.equal(client.sequence, 8);
    assert.equal(ctx.voice.stats.transmitDrops, 3);
    assert.equal(ctx.voice.stats.uplinkFrames, 8);
    assert.equal(client.timestampUs, 380000n);
    assert.equal(ctx.voice.stats.skippedMediaUs, 220000);
    assert.equal(unpackFrame(client.writes[0]).timestampUs, 160000n);
    assert.equal(unpackFrame(client.writes[7]).timestampUs, 300000n);
    assert.equal(client.state, "open");
    assert.equal(ctx.media.created[0].stopped, false);
  } finally {
    await ctx.voice.stop();
  }
});

await check("uplink-budget-closes-when-the-queue-has-no-bound", async () => {
  const client = liveClient();
  delete client.socket.bufferedAmount;
  const ctx = boot({client});
  try {
    await ctx.start;
    ctx.audio.samples(new Float32Array(320), 16000);
    await flush();
    assert.equal(client.writes.length, 0);
    assert.equal(client.sequence, 0);
    assert.equal(client.state, "closed");
    assert.equal(ctx.media.created[0].stopped, true);
    assert.ok(names(ctx.statuses).includes("uplink-stalled"));
    assert.equal(ctx.voice.stats.transmitDrops, 1);
  } finally {
    await ctx.voice.stop();
  }
});

await check("uplink-budget-closes-at-the-hard-cap", async () => {
  const client = liveClient();
  client.socket.bufferedAmount = UPLINK_CLOSE_QUEUED_BYTES;
  const ctx = boot({client});
  try {
    await ctx.start;
    ctx.audio.samples(new Float32Array(320), 16000);
    await flush();
    assert.equal(client.writes.length, 0);
    assert.equal(client.state, "closed");
    assert.equal(ctx.media.created[0].stopped, true);
    assert.equal(client.socket.readyState, 3);
  } finally {
    await ctx.voice.stop();
  }
});

await check("handshake-failure-closes-the-adopted-socket", async () => {
  const client = liveClient();
  let closes = 0;
  const realClose = client.close.bind(client);
  client.close = () => {
    closes += 1;
    realClose();
  };
  const ctx = boot({
    client,
    connect: async (lease) => {
      lease.adopt(client);
      throw Object.assign(new Error("handshake failed"), {code: "handshake_failed"});
    },
  });
  try {
    await assert.rejects(ctx.start, (error) => error.code === "handshake_failed");
    assert.equal(closes, 1);
    assert.equal(client.state, "closed");
    assert.equal(client.socket.readyState, 3);
    assert.equal(ctx.media.created[0].stopped, true);
    assert.equal(ctx.audio.closed, 1);
    assert.equal(client.writes.length, 0);
  } finally {
    await ctx.voice.stop();
  }
});

await check("streaming-resampler-matches-a-one-shot-24k-buffer", async () => {
  const total = GEMINI_LIVE_OUTPUT_RATE_HZ;
  const samples = new Float32Array(total);
  for (let i = 0; i < total; i += 1) samples[i] = Math.sin(i / 17) * 0.4;
  const pcm = floatToS16le(samples);
  const once = resampleS16leToAvip(pcm, GEMINI_LIVE_OUTPUT_RATE_HZ);
  const streaming = new S16leResampler(GEMINI_LIVE_OUTPUT_RATE_HZ, 16000);
  const parts = [];
  for (let offset = 0; offset < pcm.length; offset += 960) {
    parts.push(streaming.push(pcm.subarray(offset, offset + 960)));
  }
  const merged = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
  let cursor = 0;
  for (const part of parts) {
    merged.set(part, cursor);
    cursor += part.length;
  }
  assert.equal(merged.length, once.length);
  assert.deepEqual(merged, once);
  assert.ok(Math.abs(merged.length / 2 - 16000) < 8);
});

await check("gemini-24k-pcm-is-not-played-as-avip-16k", async () => {
  const oneSecond = floatToS16le(new Float32Array(GEMINI_LIVE_OUTPUT_RATE_HZ).fill(0.25));
  assert.equal(oneSecond.length / 2 / 16000, 1.5);
  assert.throws(() => playbackSchedule(oneSecond, GEMINI_LIVE_OUTPUT_RATE_HZ), (error) => {
    assert.equal(error.code, "sample_rate_mismatch");
    assert.match(error.message, /24000/);
    assert.match(error.message, /16000/);
    return true;
  });
  const avip = resampleS16leToAvip(oneSecond, GEMINI_LIVE_OUTPUT_RATE_HZ);
  assert.equal(avip.length / 2, 16000);
  const scheduled = playbackSchedule(avip, 16000);
  assert.equal(scheduled.sampleRateHz, 16000);
  assert.equal(scheduled.durationSec, 1);
  assert.ok(s16leToFloat(avip).every((value) => Math.abs(value - 0.25) < 0.002));
  const ctx = boot({downlinkRateHz: GEMINI_LIVE_OUTPUT_RATE_HZ});
  try {
    await ctx.start;
    pushDownlink(ctx.client, oneSecond.slice(0, 640));
    await flush();
    assert.equal(ctx.audio.played.length, 0);
    assert.equal(ctx.voice.stats.playbackFrames, 0);
    assert.equal(ctx.voice.stats.playbackDrops, 1);
    assert.equal(ctx.voice.metrics["playback-start"], undefined);
    assert.ok(names(ctx.statuses).includes("playback-failed"));
    assert.equal(ctx.client.state, "open");
  } finally {
    await ctx.voice.stop();
  }
});

await check("telemetry-report-uses-the-session-clock", async () => {
  const ctx = boot({wallNow: () => 1700000000000});
  try {
    await ctx.start;
    for (let index = 0; index < 10; index += 1) {
      ctx.clock.value = 20 + index * 20;
      ctx.audio.samples(new Float32Array(320), 16000);
      ctx.clock.value = 25 + index * 20;
      pushDownlink(ctx.client);
      await flush();
    }
    const report = ctx.voice.telemetry.report();
    assert.equal(report.schema, "avi.voice-telemetry/1");
    assert.equal(report.path, "mock-mirror");
    assert.equal(report.client, "browser");
    assert.equal(report.scenario, "page");
    assert.equal(report.clocks.session.name, "local");
    assert.equal(report.clocks.session.scope, "page-or-process");
    assert.equal(report.clocks.session.comparableAcrossHosts, false);
    assert.match(report.clocks.session.description, /performance\.now/);
    assert.match(report.clocks.media.description, /Do not subtract/);
    assert.equal(report.clocks.wall.anchorSessionMs, 10);
    assert.equal(report.clocks.wall.anchorWallMs, 1700000000000);
    assert.equal(report.marks["capture-start"].sessionMs, 10);
    assert.equal(report.marks["session-start"].sessionMs, 10);
    assert.equal(report.marks["first-uplink"].sessionMs, 20);
    assert.equal(report.marks["first-downlink"].sessionMs, 25);
    assert.equal(report.marks["playback-start"].sessionMs, 25);
    assert.equal(report.intervalsMs.captureToUplink, 10);
    assert.equal(report.intervalsMs.uplinkToDownlink, 5);
    assert.equal(report.intervalsMs.downlinkToAudible, 0);
    assert.equal(report.counters.uplinkFrames, 10);
    assert.equal(report.counters.downlinkFrames, 10);
    assert.equal(report.counters.playbackFrames, 8);
    assert.equal(report.counters.speakerDrops, 2);
    assert.equal(report.counters.captureDrops, 0);
    assert.equal(report.e2e.clock, "session");
    assert.equal(report.e2e.scope, "mock-mirror");
    assert.equal(report.e2e.uplinkToDownlink.count, 10);
    assert.equal(report.e2e.uplinkToDownlink.min, 5);
    assert.equal(report.e2e.uplinkToDownlink.p95, 5);
    assert.equal(report.e2e.uplinkToAudible.count, 8);
    assert.equal(report.cost.provider, null);
    assert.equal(report.cost.amount, null);
    assert.equal(report.cost.inputTokens, null);
    assert.equal(JSON.stringify(report).includes("AIza"), false);
    assert.equal(JSON.stringify(report).toLowerCase().includes("hermes"), false);
    ctx.voice.telemetry.noteCost({provider: "gemini", apiKey: "AIzaSyTESTSECRETVALUE", amount: 1});
    const refused = ctx.voice.telemetry.report();
    assert.equal(refused.cost.provider, null);
    assert.equal(refused.cost.amount, null);
    assert.equal(JSON.stringify(refused).includes("AIzaSyTESTSECRETVALUE"), false);
  } finally {
    await ctx.voice.stop();
  }
});

await check("capture-worklet-registers-the-avip-processor", async () => {
  let registered = null;
  globalThis.AudioWorkletProcessor = class {};
  globalThis.registerProcessor = (name) => {
    registered = name;
  };
  const source = readFileSync(new URL("./capture-worklet.js", import.meta.url), "utf8");
  new Function(source)();
  assert.equal(registered, "avip-capture");
  const voiceSource = readFileSync(new URL("./voice.js", import.meta.url), "utf8");
  assert.match(voiceSource, /from "\.\/avip-client\.js"/);
  assert.doesNotMatch(voiceSource, /new WebSocket/);
  assert.doesNotMatch(voiceSource, /playback\.stop/);
  assert.doesNotMatch(voiceSource, /hermes/i);
});

const failed = checks.filter((item) => !item.ok);
process.stdout.write(JSON.stringify({ok: failed.length === 0, checks}));
if (failed.length) process.exit(1);
