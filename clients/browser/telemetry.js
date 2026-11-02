// Local voice-session measurements for the browser client.
// This file does not open a socket, a microphone, or a provider, and it does
// not read API keys. The report is JSON for a page, a dashboard, or a CI
// artifact. It is not an AVIP message.
//
// Session clock: monotonic milliseconds on this page. BrowserVoice reports it
// as clock "local" (performance.now, or the injected now). Deltas between
// marks in one report are valid. Do not subtract it from another host, from
// the wall-clock anchor, or from an AVIP media timestamp.
//
// Wall clock: Unix-epoch milliseconds captured once when this object is
// constructed, paired with the session clock at that instant. Not for latency.
//
// Media clock: AVIP timestamp_us, the sender clock in microseconds, starting
// at 0 per direction. The mock mirror's downlink timestamp is the gateway's
// media clock. Do not subtract it from the session clock.
//
// mock-mirror end-to-end samples pair the Nth uplink send with the Nth
// downlink on the session clock. That is loopback, not a model latency.
// cost stays null until a provider calls noteCost.

// Keep this twin in sync with src/avi/telemetry.py (SCHEMA).
// The Node/Python soak equality test fails if the report shape drifts.
export const SCHEMA = "avi.voice-telemetry/1";
export const VOICE_MARKS = [
  "capture-start",
  "session-start",
  "first-uplink",
  "first-downlink",
  "playback-start",
];
export const SAMPLE_CAP = 4096;
const COUNTERS = [
  "uplinkFrames",
  "downlinkFrames",
  "playbackFrames",
  "captureDrops",
  "transmitDrops",
  "speakerDrops",
  "timeouts",
  "queueFull",
];
const COST_FIELDS = [
  "provider",
  "currency",
  "inputTokens",
  "outputTokens",
  "audioInputSeconds",
  "audioOutputSeconds",
  "amount",
];
const COST_STRINGS = new Set(["provider", "currency"]);
const BUCKETS_MS = [5, 10, 20, 40, 80, 160, 320, 640, 1280];
const NOMINAL_FRAME_MS = 20;
const CLIENTS = new Set(["browser", "headless", "gateway"]);
const PATHS = new Set(["mock-mirror", "provider"]);
const KIND = /^[a-z][a-z0-9-]{0,32}$/;
const TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
const SESSION_ID = /^s[0-9a-f]{32}$/;
const FORBIDDEN_KEY = /key|secret|token|password|authorization|credential/i;
const SECRET = /api[_-]?key|secret|bearer\s|AIza[0-9A-Za-z_-]{8,}|sk-[A-Za-z0-9]{8,}/i;
const INTERVALS = [
  ["captureToSession", "capture-start", "session-start"],
  ["captureToUplink", "capture-start", "first-uplink"],
  ["sessionToUplink", "session-start", "first-uplink"],
  ["uplinkToDownlink", "first-uplink", "first-downlink"],
  ["uplinkToAudible", "first-uplink", "playback-start"],
  ["downlinkToAudible", "first-downlink", "playback-start"],
];

const SESSION_CLOCK_TEXT = (
  "Monotonic milliseconds on one page or one process. " +
  'The browser hook reports this clock as "local" (performance.now, ' +
  "milliseconds since the page time origin). " +
  "The gateway and the headless harness use time.monotonic converted to milliseconds. " +
  "Deltas between marks in one report are valid. " +
  "Do not subtract this clock from another host, from the wall-clock anchor, " +
  "or from an AVIP media timestamp."
);
const WALL_CLOCK_TEXT = (
  "Unix-epoch milliseconds captured once, paired with the session clock when " +
  "telemetry is constructed. The anchor can place this report on a timeline. " +
  "Do not subtract wall clocks across hosts, and do not compute latency from the wall clock."
);
const MEDIA_CLOCK_TEXT = (
  "AVIP timestamp_us is the sender's media clock in microseconds. " +
  "It starts at 0 in each direction. " +
  "The mock mirror's downlink timestamp is the gateway media clock, not the client's. " +
  "Do not subtract a media timestamp from the session clock, from the other direction, " +
  "or across hosts."
);

function defaultNow() {
  if (typeof performance !== "undefined" && typeof performance.now === "function") return performance.now();
  return Date.now();
}

function scrub(value) {
  if (typeof value === "string") return SECRET.test(value) ? "[redacted]" : value;
  if (Array.isArray(value)) return value.map((item) => scrub(item));
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value)) out[key] = scrub(value[key]);
    return out;
  }
  if (typeof value === "number" && !Number.isFinite(value)) return null;
  return value;
}

function asCount(value) {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) return null;
  return value;
}

function percentile(samples, fraction) {
  if (!samples.length) return null;
  const ordered = [...samples].sort((left, right) => left - right);
  let rank = Math.ceil(fraction * ordered.length) - 1;
  rank = Math.min(Math.max(rank, 0), ordered.length - 1);
  return ordered[rank];
}

class Distribution {
  constructor(sampleCap) {
    this.sampleCap = sampleCap;
    this.count = 0;
    this.sum = 0;
    this.min = null;
    this.max = null;
    this.samples = [];
    this.approximate = false;
    this.histogram = BUCKETS_MS.map(() => 0);
    this.histogram.push(0);
  }

  record(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return;
    this.count += 1;
    this.sum += value;
    this.min = this.min === null ? value : Math.min(this.min, value);
    this.max = this.max === null ? value : Math.max(this.max, value);
    if (this.samples.length < this.sampleCap) {
      this.samples.push(value);
    } else {
      const slot = Math.floor(Math.random() * this.count);
      if (slot < this.sampleCap) this.samples[slot] = value;
      this.approximate = true;
    }
    let placed = BUCKETS_MS.length;
    for (let index = 0; index < BUCKETS_MS.length; index += 1) {
      if (value <= BUCKETS_MS[index]) {
        placed = index;
        break;
      }
    }
    this.histogram[placed] += 1;
  }

  snapshot() {
    const histogram = BUCKETS_MS.map((edge, index) => ({leMs: edge, count: this.histogram[index]}));
    histogram.push({leMs: null, count: this.histogram[BUCKETS_MS.length]});
    return {
      count: this.count,
      min: this.min,
      max: this.max,
      mean: this.count === 0 ? null : this.sum / this.count,
      p50: percentile(this.samples, 0.5),
      p95: percentile(this.samples, 0.95),
      approximate: this.approximate,
      sampleCap: this.sampleCap,
      histogram,
    };
  }
}

export class VoiceTelemetry {
  constructor({
    now = defaultNow,
    wallNow = () => Date.now(),
    client,
    path = "mock-mirror",
    scenario,
    sampleCap = SAMPLE_CAP,
  } = {}) {
    this.now = now;
    this.wallNow = wallNow;
    this.client = CLIENTS.has(client) ? client : "unknown";
    this.path = PATHS.has(path) ? path : "mock-mirror";
    this.scenario = typeof scenario === "string" && TOKEN.test(scenario) ? scenario : "unknown";
    this.anchorSessionMs = now();
    this.anchorWallMs = wallNow();
    this.marks = {};
    this.counters = Object.fromEntries(COUNTERS.map((name) => [name, 0]));
    this.cost = Object.fromEntries(COST_FIELDS.map((name) => [name, null]));
    this.timeouts = [];
    this.sessionId = null;
    this.depth = null;
    this.depthMax = null;
    this.uplinkQueuedMax = null;
    this.sinkHighWater = null;
    this.sinkConsumed = null;
    this.e2e = new Distribution(sampleCap);
    this.audible = new Distribution(sampleCap);
    this.jitter = new Distribution(sampleCap);
    this.uplinkAt = [];
    this.pair = 0;
    this.lastDownlink = null;
    this.lastPairedUplink = null;
  }

  mark(name, sessionMs) {
    if (!VOICE_MARKS.includes(name) || Object.prototype.hasOwnProperty.call(this.marks, name)) return;
    this.marks[name] = this._at(sessionMs);
  }

  setSessionId(value) {
    if (typeof value === "string" && SESSION_ID.test(value)) this.sessionId = value;
  }

  addCounter(name, amount = 1) {
    if (!Object.prototype.hasOwnProperty.call(this.counters, name) || asCount(amount) === null) return;
    this.counters[name] += amount;
  }

  setCounters(values) {
    if (!values || typeof values !== "object") return;
    for (const name of COUNTERS) {
      if (!Object.prototype.hasOwnProperty.call(values, name)) continue;
      const parsed = asCount(values[name]);
      if (parsed !== null) this.counters[name] = parsed;
    }
  }

  noteUplink(sessionMs) {
    this.uplinkAt.push(this._at(sessionMs));
  }

  noteDownlink(sessionMs) {
    const at = this._at(sessionMs);
    if (this.lastDownlink !== null) this.jitter.record(Math.abs(at - this.lastDownlink - NOMINAL_FRAME_MS));
    this.lastDownlink = at;
    if (this.pair < this.uplinkAt.length) {
      const sent = this.uplinkAt[this.pair];
      this.pair += 1;
      this.lastPairedUplink = sent;
      const delay = at - sent;
      if (delay >= 0) this.e2e.record(delay);
    } else {
      this.lastPairedUplink = null;
    }
    if (this.pair > 64) {
      this.uplinkAt = this.uplinkAt.slice(this.pair);
      this.pair = 0;
    }
  }

  noteAudible(sessionMs) {
    if (this.lastPairedUplink === null) return;
    const delay = this._at(sessionMs) - this.lastPairedUplink;
    if (delay >= 0) this.audible.record(delay);
  }

  noteBufferDepth(frames) {
    if (typeof frames !== "number" || !Number.isFinite(frames) || frames < 0) return;
    const value = Math.round(frames * 1e6) / 1e6;
    this.depth = value;
    this.depthMax = this.depthMax === null ? value : Math.max(this.depthMax, value);
  }

  noteUplinkQueued(bufferedAmount) {
    if (typeof bufferedAmount !== "number" || !Number.isInteger(bufferedAmount) || bufferedAmount < 0) return;
    if (this.uplinkQueuedMax === null || bufferedAmount > this.uplinkQueuedMax) {
      this.uplinkQueuedMax = bufferedAmount;
    }
  }

  noteSink(highWater, consumed) {
    if (asCount(highWater) !== null) this.sinkHighWater = highWater;
    if (asCount(consumed) !== null) this.sinkConsumed = consumed;
  }

  noteQueueFull() {
    this.counters.queueFull += 1;
  }

  noteTimeout(kind) {
    const token = typeof kind === "string" && KIND.test(kind) ? kind : "other";
    this.counters.timeouts += 1;
    if (this.timeouts.length < 32) this.timeouts.push({kind: token, sessionMs: this.now()});
  }

  noteCost(fields) {
    if (!fields || typeof fields !== "object") return;
    for (const key of Object.keys(fields)) {
      if (!COST_FIELDS.includes(key) && FORBIDDEN_KEY.test(key)) return;
    }
    const cleaned = {};
    for (const key of COST_FIELDS) {
      if (!Object.prototype.hasOwnProperty.call(fields, key)) continue;
      const value = fields[key];
      if (value === null) {
        cleaned[key] = null;
        continue;
      }
      if (COST_STRINGS.has(key)) {
        if (typeof value !== "string" || !TOKEN.test(value) || SECRET.test(value)) return;
        cleaned[key] = value;
        continue;
      }
      if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return;
      cleaned[key] = value;
    }
    Object.assign(this.cost, cleaned);
  }

  report() {
    const marks = {};
    for (const name of VOICE_MARKS) {
      if (Object.prototype.hasOwnProperty.call(this.marks, name)) marks[name] = {sessionMs: this.marks[name]};
    }
    const intervalsMs = {};
    for (const [label, start, end] of INTERVALS) {
      intervalsMs[label] = (
        Object.prototype.hasOwnProperty.call(this.marks, start) &&
        Object.prototype.hasOwnProperty.call(this.marks, end)
      ) ? this.marks[end] - this.marks[start] : null;
    }
    return scrub({
      schema: SCHEMA,
      path: this.path,
      client: this.client,
      scenario: this.scenario,
      sessionId: this.sessionId,
      clocks: {
        session: {
          name: "local",
          unit: "ms",
          scope: "page-or-process",
          comparableAcrossHosts: false,
          description: SESSION_CLOCK_TEXT,
        },
        wall: {
          unit: "ms",
          scope: "unix-epoch",
          comparableAcrossHosts: false,
          anchorSessionMs: this.anchorSessionMs,
          anchorWallMs: this.anchorWallMs,
          description: WALL_CLOCK_TEXT,
        },
        media: {
          unit: "us",
          scope: "avip-sender",
          comparableAcrossHosts: false,
          description: MEDIA_CLOCK_TEXT,
        },
      },
      marks,
      intervalsMs,
      counters: {...this.counters},
      buffer: {
        depthFrames: this.depth,
        depthMaxFrames: this.depthMax,
        uplinkQueuedBytesMax: this.uplinkQueuedMax,
        jitter: this.jitter.snapshot(),
        sinkHighWater: this.sinkHighWater,
        sinkConsumed: this.sinkConsumed,
      },
      e2e: {
        scope: this.path === "provider" ? "provider" : "mock-mirror",
        unit: "ms",
        clock: "session",
        uplinkToDownlink: this.e2e.snapshot(),
        uplinkToAudible: this.audible.snapshot(),
      },
      cost: {...this.cost},
      timeouts: this.timeouts.map((item) => ({...item})),
    });
  }

  _at(sessionMs) {
    return sessionMs === undefined ? this.now() : sessionMs;
  }
}
