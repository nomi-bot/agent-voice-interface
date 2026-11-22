import assert from "node:assert/strict";
import {existsSync, readFileSync} from "node:fs";
import {resolve} from "node:path";
import test from "node:test";
import {pathToFileURL} from "node:url";

import {AUDIO_DOWNLINK, AvipBrowserClient, packFrame, unpackFrame} from "./avip-client.js";
import {MAX_BUFFER_FRAMES, SAMPLE_RATE_HZ, s16leToFloat} from "./pcm.js";
import * as browser from "./voice.js";

const PROTOCOL = "avip/1";
const FRAME_MS = 20;
const SAMPLES = SAMPLE_RATE_HZ * FRAME_MS / 1000;
const AUDIO = {audio_input: true, audio_output: true};
const PCM = new Uint8Array(SAMPLES * 2).fill(12);
const CAPTURE = new Float32Array(SAMPLES).fill(0.2);
const turn = () => new Promise((resolveTurn) => setImmediate(resolveTurn));

class ScriptedSocket extends EventTarget {
  readyState = 0;
  bufferedAmount = 0;

  constructor() {
    super();
    queueMicrotask(() => {
      if (this.readyState !== 0) return;
      this.readyState = 1;
      this.dispatchEvent(new Event("open"));
    });
  }

  send(data) {
    assert.equal(this.readyState, 1);
    this.onSend(data);
  }

  receive(message) {
    if (this.readyState === 1) {
      const data = message instanceof Uint8Array ? message : JSON.stringify(message);
      this.dispatchEvent(new MessageEvent("message", {data}));
    }
  }

  close(code = 1000, reason = "") {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.dispatchEvent(Object.assign(new Event("close"), {code, reason}));
  }
}

globalThis.WebSocket = ScriptedSocket;

function envelope(type, fields = {}) {
  return {protocol: PROTOCOL, type, ...fields};
}

function connection({id = "initial", capabilities = AUDIO, onSend = () => {}} = {}) {
  const client = new AvipBrowserClient({
    url: "ws://127.0.0.1:8000/ws/avip",
    requestId: `request-${id}`,
    deviceId: "browser-fixture",
    capabilities,
  });
  const connect = async (lease) => {
    lease?.adopt(client);
    await client.connect();
    client.socket.onSend = (data) => {
      const frame = typeof data === "string" ? JSON.parse(data) : unpackFrame(data);
      if (frame.type === "session.start") {
        assert.equal(frame.session_id, null);
        client.socket.receive(envelope("session.ready", {
          request_id: frame.request_id,
          session_id: `session-${id}`,
          capabilities,
        }));
      } else {
        onSend(frame, client.socket);
      }
    };
    return {client, ready: await client.start()};
  };
  return {client, connect};
}

function voiceFixture({module = browser, link = connection(), getUserMedia, opening, closing} = {}) {
  const clock = {ms: 0};
  const track = new EventTarget();
  track.readyState = "live";
  track.stop = () => { track.readyState = "ended"; };
  const stream = {getTracks: () => [track], getAudioTracks: () => [track]};
  const speaker = new module.SpeakerBuffer({
    now: () => clock.ms / 1000,
    play(payload, at) {
      const end = at * 1000 + payload.length / 2 / SAMPLE_RATE_HZ * 1000;
      return {
        get ended() { return this.stoppedAt !== undefined || clock.ms + 1e-6 >= end; },
        stop() { this.stoppedAt = clock.ms; },
      };
    },
  });
  const audio = {
    begin() {},
    async open({onSamples}) {
      await opening;
      audio.opened = true;
      audio.capture = (samples = CAPTURE) => onSamples(samples, SAMPLE_RATE_HZ);
      return {speaker};
    },
    async close() {
      await closing;
      audio.opened = false;
    },
  };
  const statuses = [];
  const voice = new module.BrowserVoice({
    audio,
    now: () => clock.ms,
    onStatus(name) { statuses.push(name); },
    media: {
      location: () => new URL("http://127.0.0.1:8000/voice"),
      getUserMedia: getUserMedia ? () => getUserMedia(stream) : async () => stream,
    },
  });
  return {audio, clock, link, speaker, statuses, track, voice};
}

function downlink(client, sequence, payload = PCM) {
  client.socket.receive(packFrame(AUDIO_DOWNLINK, sequence, sequence * FRAME_MS * 1000, payload));
}

const checkout = process.env.AVI_DUPLEX_GENERATION_CHECKOUT;
const generationBrowser = checkout
  ? await import(pathToFileURL(resolve(checkout, "clients/browser/voice.js")))
  : browser;
if (checkout) assert.equal(typeof generationBrowser.applyPlaybackInterruption, "function");

test("stop before microphone permission never opens a session", async () => {
  const permission = Promise.withResolvers();
  let connectCalls = 0;
  const ctx = voiceFixture({getUserMedia: async (stream) => { await permission.promise; return stream; }});
  const started = ctx.voice.start({connect(lease) { connectCalls += 1; return ctx.link.connect(lease); }});
  try {
    await ctx.voice.stop();
    permission.resolve();
    await assert.rejects(started, {code: "session_closed"});
    assert.equal(connectCalls, 0);
    assert.equal(ctx.track.readyState, "ended");
    assert.equal(ctx.voice.running, false);
  } finally {
    ctx.link.client.close();
    await ctx.voice.stop();
  }
});

test("stop during audio open releases the late speaker and remains stopped", async () => {
  const opening = Promise.withResolvers();
  const ctx = voiceFixture({opening: opening.promise});
  const started = ctx.voice.start({connect: ctx.link.connect});
  try {
    await turn();
    assert.equal(ctx.link.client.started, true);
    await ctx.voice.stop();
    opening.resolve();
    await assert.rejects(started, {code: "session_closed"});
    assert.equal(ctx.track.readyState, "ended");
    assert.equal(ctx.audio.opened, false);
    assert.equal(ctx.link.client.state, "closed");
    assert.equal(ctx.voice.running, false);
  } finally {
    ctx.link.client.close();
    await ctx.voice.stop();
  }
});

test("shutdown closes the socket while output cleanup is stalled", async () => {
  const closing = Promise.withResolvers();
  const ctx = voiceFixture({closing: closing.promise});
  await ctx.voice.start({connect: ctx.link.connect});
  const stopped = ctx.voice.stop();
  try {
    assert.equal(ctx.link.client.state, "closed");
    assert.equal(ctx.track.readyState, "ended");
  } finally {
    closing.resolve();
    await stopped;
  }
});

test("a stalled speaker bounds handles even with small legal PCM frames", async () => {
  const ctx = voiceFixture();
  await ctx.voice.start({connect: ctx.link.connect});
  try {
    for (let index = 0; index < MAX_BUFFER_FRAMES * 3; index += 1) {
      downlink(ctx.link.client, index, Uint8Array.of(1, 0));
      await turn();
    }
    const sourceLimit = MAX_BUFFER_FRAMES + 1;
    assert.equal(ctx.voice.stats.playbackFrames, sourceLimit);
    assert.equal(ctx.voice.stats.playbackDrops, MAX_BUFFER_FRAMES * 3 - sourceLimit);
    ctx.clock.ms += 20;
    downlink(ctx.link.client, MAX_BUFFER_FRAMES * 3);
    await turn();
    assert.equal(ctx.voice.stats.playbackFrames, sourceLimit + 1);
  } finally {
    await ctx.voice.stop();
  }
});

test("elapsed schedules free the source budget even with delayed end notifications", () => {
  let now = 0;
  const speaker = new browser.SpeakerBuffer({now: () => now, play: () => ({ended: false, stop() {}})});
  const frames = 3_000 / FRAME_MS;
  for (let index = 0; index < frames; index += 1) {
    now = Math.max(0, index + 1 - MAX_BUFFER_FRAMES) * FRAME_MS / 1000;
    assert.equal(speaker.enqueue(PCM), true, `frame ${index}`);
    assert.ok(speaker.handles.length <= MAX_BUFFER_FRAMES);
  }
  now += 10;
  assert.equal(speaker.enqueue(PCM), true);
  assert.equal(speaker.handles.length, 1);
  assert.equal(speaker.played, frames + 1);
  assert.equal(speaker.dropped, 0);
});

test("inbound overflow closes capture and records the queue failure", async () => {
  const ctx = voiceFixture();
  await ctx.voice.start({connect: ctx.link.connect});
  try {
    for (let index = 0; index < MAX_BUFFER_FRAMES + 2; index += 1) {
      downlink(ctx.link.client, index);
    }
    await turn();
    assert.equal(ctx.link.client.state, "closed");
    assert.equal(ctx.link.client.pending, 0);
    assert.equal(ctx.track.readyState, "ended");
    assert.equal(ctx.voice.telemetry.report().counters.queueFull, 1);
  } finally {
    await ctx.voice.stop();
  }
});

test("local playback stop keeps capture running through a sender stall", async () => {
  const uplink = [];
  const link = connection({onSend(frame) { uplink.push(frame); }});
  const ctx = voiceFixture({link});
  await ctx.voice.start({connect: link.connect});
  try {
    downlink(link.client, 0);
    await turn();
    const [handle] = ctx.speaker.handles;
    ctx.voice.stopPlayback();
    assert.equal(handle.stoppedAt, 0);
    assert.equal(ctx.track.readyState, "live");
    ctx.audio.capture();
    link.client.socket.bufferedAmount = browser.MAX_UPLINK_QUEUED_BYTES;
    ctx.audio.capture();
    ctx.audio.capture();
    link.client.socket.bufferedAmount = 0;
    ctx.audio.capture();
    downlink(link.client, 1);
    await turn();
    assert.deepEqual(uplink.map(({sequence, timestampUs}) => [sequence, timestampUs]), [[0, 0n], [1, 60_000n]]);
    assert.equal(ctx.voice.stats.transmitDrops, 2);
    assert.equal(ctx.voice.stats.playbackFrames, 1);
    assert.equal(link.client.state, "open");
  } finally {
    await ctx.voice.stop();
  }
});

test("gateway GoAway close releases capture and output on the old session", async () => {
  const ctx = voiceFixture();
  await ctx.voice.start({connect: ctx.link.connect});
  try {
    downlink(ctx.link.client, 0);
    await turn();
    const [handle] = ctx.speaker.handles;
    ctx.link.client.socket.close(1011, "Voice provider unavailable");
    await turn();
    assert.equal(ctx.track.readyState, "ended");
    assert.equal(handle.ended, true);
    assert.equal(ctx.audio.opened, false);
    assert.equal(ctx.voice.running, false);
    assert.ok(ctx.statuses.includes("disconnected"));
    ctx.audio.capture();
    assert.equal(ctx.voice.stats.uplinkFrames, 0);
  } finally {
    await ctx.voice.stop();
  }
});

test("provider interruption flushes active output and late interrupts leave the next turn intact", {
  skip: typeof generationBrowser.applyPlaybackInterruption !== "function" && "requires JEV-32 browser handler",
}, async (t) => {
  const uplink = [];
  const link = connection({onSend(frame) { uplink.push(frame); }});
  const ctx = voiceFixture({module: generationBrowser, link});
  const interrupt = (generation) => link.client.socket.receive(envelope("response.interrupted", {generation}));
  await ctx.voice.start({connect: link.connect});
  try {
    downlink(link.client, 0);
    await turn();
    const [first] = ctx.speaker.handles;
    ctx.clock.ms = 4;
    const syntheticSpeechAt = ctx.clock.ms;
    ctx.audio.capture(s16leToFloat(PCM));
    ctx.clock.ms += 8;
    interrupt(0);
    await turn();
    assert.equal(first.stoppedAt - syntheticSpeechAt, 8);
    assert.equal(ctx.speaker.handles.length, 0);
    assert.equal(ctx.track.readyState, "live");
    ctx.clock.ms += 10_000;
    ctx.audio.capture();
    interrupt(1); // Idle provider signal while generation 1 has no audio.
    downlink(link.client, 1);
    await turn();
    const [next] = ctx.speaker.handles;
    assert.ok(next);
    interrupt(0);
    interrupt(1);
    await turn();
    assert.deepEqual(ctx.speaker.handles, [next]);
    assert.equal(next.ended, false);
    assert.deepEqual(uplink.map(({sequence}) => sequence), [0, 1]);
    assert.equal(ctx.voice.stats.playbackFrames, 2);
    t.diagnostic(JSON.stringify({clock: "virtual", scheduleStopMs: 8, count: 1}));
  } finally {
    await ctx.voice.stop();
  }
});

test("paced three-minute virtual session keeps queues bounded across silent gaps", async (t) => {
  const delays = [];
  let sent = 0;
  let silentCaptures = 0;
  let maxScheduled = 0;
  let sequence = 0;
  const link = connection({onSend(frame) {
    assert.equal(frame.sequence, sent);
    assert.equal(frame.timestampUs, BigInt(sent * FRAME_MS * 1000));
    sent += 1;
  }});
  const ctx = voiceFixture({link});
  await ctx.voice.start({connect: link.connect});
  try {
    for (let i = 0; i < 9_000; i += 1) {
      const captureAt = i * FRAME_MS;
      ctx.clock.ms = captureAt;
      ctx.audio.capture();
      if (i % 1500 >= 1450) {
        silentCaptures += 1;
        continue;
      }
      ctx.clock.ms += 2 + i % 7;
      downlink(link.client, sequence);
      sequence += 1;
      await turn();
      maxScheduled = Math.max(maxScheduled, ctx.speaker.handles.length);
      delays.push(ctx.speaker.nextTime * 1000 - FRAME_MS - captureAt);
    }
    assert.equal(sent, 9_000);
    assert.equal(silentCaptures, 300);
    assert.equal(ctx.voice.stats.playbackFrames, sent - silentCaptures);
    assert.equal(ctx.voice.stats.transmitDrops + ctx.voice.stats.playbackDrops, 0);
    assert.ok(maxScheduled <= MAX_BUFFER_FRAMES);
    assert.equal(link.client.pending, 0);
    assert.equal(link.client.state, "open");
    delays.sort((a, b) => a - b);
    t.diagnostic(JSON.stringify({clock: "virtual", sessionMs: 180_000, captures: sent, played: delays.length,
      maxScheduled, p50ScheduleMs: Math.round(delays[Math.ceil(delays.length * 0.50) - 1]),
      p95ScheduleMs: Math.round(delays[Math.ceil(delays.length * 0.95) - 1]), dropped: 0}));
  } finally {
    await ctx.voice.stop();
  }
});

const RUN = "agent.run.";
const RUN_ID = "run-duplex-1";
const TASK = {request_id: "request-task-1", kind: "read_only", arguments: {query: "Synthetic continuity."}};
const OWNER_GRANT = "a".repeat(43);
const EXPIRED_GRANT = "e".repeat(43);
const DURABLE_AUDIO = {...AUDIO, durable_runs: true};
const DENIED = envelope("error", {code: "access_denied", field: null});
const runMessage = (type, fields) => envelope(`${RUN}${type}`, fields);

function runStatus(runId, state, cursor) {
  return {run_id: runId, state, dispatch: "accepted", cursor, expires_at: 700,
    expired: false, failure_code: null, reconciliation_pending: false};
}

async function scriptedRunSession(id, steps, capabilities = DURABLE_AUDIO) {
  const link = connection({id, capabilities, onSend(frame, socket) {
    const step = steps.shift();
    assert.ok(step, "unexpected command or retransmission");
    assert.deepEqual(frame, step.request);
    for (const reply of step.replies) socket.receive(reply);
  }});
  const {ready} = await link.connect();
  return {
    ready,
    client: link.client,
    send(message) { link.client.socket.send(JSON.stringify(message)); },
    done() {
      link.client.close();
      assert.equal(steps.length, 0, "some expected commands were not sent");
    },
  };
}

function exchange(type, fields, ...replies) {
  return {request: runMessage(type, fields), replies};
}

async function attach(session, runId, cursor, seen) {
  session.send(runMessage("attach", {run_id: runId, after_cursor: cursor}));
  while (true) {
    const message = await session.client.nextFrame();
    assert.equal(message.run_id, runId);
    if (message.type === `${RUN}replay`) {
      assert.equal(message.resume_cursor, cursor);
      return cursor;
    }
    assert.equal(message.type, `${RUN}event`);
    if (message.cursor <= cursor) continue;
    assert.equal(message.cursor, cursor + 1);
    seen.push(message);
    cursor = message.cursor;
  }
}

test("result-send timeout keeps the old cursor for a fresh authenticated replay", async () => {
  const started = runStatus(RUN_ID, "running", 4);
  const finished = runStatus(RUN_ID, "completed", 6);
  const progress = runMessage("event", {run_id: RUN_ID, cursor: 5, kind: "progress", payload: {text: "still running"}});
  const result = runMessage("event", {run_id: RUN_ID, cursor: 6, kind: "completed", payload: {result: "Retained fixture result."}});
  const auth = () => exchange("authenticate", {token: OWNER_GRANT}, runMessage("authenticated"));
  const receipt = runMessage("replay", {run_id: RUN_ID, resume_cursor: 6});
  const initial = await scriptedRunSession("original", [auth(),
    exchange("submit", TASK, runMessage("status", started)),
    exchange("attach", {run_id: RUN_ID, after_cursor: 4}, progress)]);
  try {
    initial.send(runMessage("authenticate", {token: OWNER_GRANT}));
    assert.equal((await initial.client.nextFrame()).type, `${RUN}authenticated`);
    initial.send(runMessage("submit", TASK));
    assert.deepEqual(await initial.client.nextFrame(), runMessage("status", started));
    initial.send(runMessage("attach", {run_id: RUN_ID, after_cursor: 4}));
    assert.deepEqual(await initial.client.nextFrame(), progress);
    const unavailableResult = initial.client.nextFrame();
    initial.client.socket.close(1013, "Send timeout");
    await assert.rejects(unavailableResult, /socket closed/);
  } finally {
    initial.done();
  }
  const expired = await scriptedRunSession("expired", [exchange("authenticate", {token: EXPIRED_GRANT}, DENIED)]);
  try {
    expired.send(runMessage("authenticate", {token: EXPIRED_GRANT}));
    await assert.rejects(expired.client.nextFrame(), /access_denied/);
    assert.equal(expired.client.state, "open");
  } finally {
    expired.done();
  }
  const resumed = await scriptedRunSession("resumed", [auth(),
    exchange("attach", {run_id: RUN_ID, after_cursor: 5}, result, receipt),
    exchange("attach", {run_id: RUN_ID, after_cursor: 6}, receipt),
    exchange("result", {run_id: RUN_ID}, runMessage("result", {status: finished, retained: true, result: result.payload.result}))]);
  try {
    assert.notEqual(resumed.ready.session_id, initial.ready.session_id);
    assert.equal(resumed.ready.capabilities.durable_runs, true);
    resumed.send(runMessage("authenticate", {token: OWNER_GRANT}));
    assert.equal((await resumed.client.nextFrame()).type, `${RUN}authenticated`);
    const seen = [progress];
    const cursor = await attach(resumed, RUN_ID, progress.cursor, seen);
    assert.equal(await attach(resumed, RUN_ID, cursor, seen), 6);
    assert.deepEqual(seen.map(({run_id, cursor: eventCursor}) => [run_id, eventCursor]), [[RUN_ID, 5], [RUN_ID, 6]]);
    resumed.send(runMessage("result", {run_id: RUN_ID}));
    const retained = await resumed.client.nextFrame();
    assert.deepEqual(retained.status, finished);
    assert.equal(retained.retained, true);
    assert.equal(retained.result, result.payload.result);
  } finally {
    resumed.done();
  }
});

test("task cancellation needs the capability and authority, and waits for terminal status", async () => {
  const runId = "run-explicit-cancel";
  const cancel = {run_id: runId};
  const noCapability = await scriptedRunSession("legacy-media", [exchange("cancel", cancel,
    envelope("error", {code: "unsupported_message_type", field: "type"}))], AUDIO);
  try {
    assert.equal(noCapability.ready.capabilities.durable_runs, undefined);
    noCapability.send(runMessage("cancel", cancel));
    await assert.rejects(noCapability.client.nextFrame(), /unsupported_message_type/);
    assert.equal(noCapability.client.state, "open");
  } finally {
    noCapability.done();
  }
  const denied = await scriptedRunSession("unauthorized", [exchange("cancel", cancel, DENIED)]);
  try {
    denied.send(runMessage("cancel", cancel));
    await assert.rejects(denied.client.nextFrame(), /access_denied/);
    assert.equal(denied.client.state, "open");
  } finally {
    denied.done();
  }
  const running = runMessage("status", runStatus(runId, "running", 4));
  const cancelled = runMessage("status", runStatus(runId, "cancelled", 5));
  const owner = await scriptedRunSession("authorized", [
    exchange("authenticate", {token: OWNER_GRANT}, runMessage("authenticated")),
    exchange("cancel", cancel, running),
    exchange("status", cancel, cancelled),
  ]);
  try {
    owner.send(runMessage("authenticate", {token: OWNER_GRANT}));
    assert.equal((await owner.client.nextFrame()).type, `${RUN}authenticated`);
    owner.send(runMessage("cancel", cancel));
    assert.deepEqual(await owner.client.nextFrame(), running);
    owner.send(runMessage("status", cancel));
    assert.deepEqual(await owner.client.nextFrame(), cancelled);
  } finally {
    owner.done();
  }
});

const durableFixturePath = process.env.AVI_DUPLEX_DURABLE_FIXTURE
  || new URL("../../tests/fixtures/jev28-durable-runs-v1.json", import.meta.url);
if (process.env.AVI_DUPLEX_DURABLE_FIXTURE || existsSync(durableFixturePath)) {
  test("JEV-28 golden positive handoff replays through the browser transport", async () => {
    const {positive} = JSON.parse(readFileSync(durableFixturePath, "utf8"));
    const session = await scriptedRunSession("jev28-handoff", [
      {request: positive.authenticate, replies: [positive.authenticated]},
      {request: positive.submit, replies: [positive.status]},
      {request: positive.attach, replies: [...positive.events, positive.receipt]},
    ], positive.ready.capabilities);
    try {
      session.send(positive.authenticate);
      assert.deepEqual(await session.client.nextFrame(), positive.authenticated);
      session.send(positive.submit);
      assert.deepEqual(await session.client.nextFrame(), positive.status);
      const seen = [];
      assert.equal(await attach(session, positive.attach.run_id, positive.attach.after_cursor, seen),
        positive.receipt.resume_cursor);
      assert.deepEqual(seen, positive.events);
    } finally {
      session.done();
    }
  });
}
