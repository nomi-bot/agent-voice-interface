// Browser AVIP v1 adapter for /ws/avip.
// Load it as a module. It does not import the gateway or the headless package.
// Microphone capture and speaker playback are voice.js. This file stays the
// serializer and the socket client.
// packFrame and encodeSessionStart are the browser serializer. Conformance
// runs this file under Node and compares the bytes with the Python SDK.
//
// Timestamps are uint64. Pass a bigint, or a safe integer. The helper clock
// stores timestampUs as a bigint so it stays exact through the full uint64
// range. There is no intentional difference from the Python frame bytes.

const PROTOCOL = "avip/1";
const HEADER_BYTES = 24;
const AUDIO_UPLINK = 1;
const AUDIO_DOWNLINK = 2;
const CODEC_PCM_S16LE = 1;
const MAX_PAYLOAD_BYTES = 8192;
const MAX_FRAME_BYTES = HEADER_BYTES + MAX_PAYLOAD_BYTES;
const NOMINAL_CHUNK_US = 20000n;
// Unread messages with no waiting receiver. One more fails the session.
const MAX_PENDING_MESSAGES = 8;
const UINT32_MAX = 0xffffffff;
const UINT64_MAX = 0xffffffffffffffffn;
const ENVELOPE_KEYS = ["protocol", "type", "request_id", "session_id", "device_id", "capabilities"];
const CAPABILITY_KEYS = ["audio_input", "audio_output", "camera", "display", "robot_actions"];
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

function fail(code, field) {
  throw new Error(field ? `${code}: ${field}` : code);
}

function timestampBigInt(value) {
  if (typeof value === "bigint") {
    if (value < 0n || value > UINT64_MAX) fail("invalid_frame", "timestamp_us");
    return value;
  }
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) {
    return BigInt(value);
  }
  fail("invalid_frame", "timestamp_us");
}

function asPayload(payload) {
  if (!(payload instanceof Uint8Array)) fail("invalid_frame", "payload");
  if (payload.length > MAX_PAYLOAD_BYTES) fail("frame_too_large", "payload");
  if (payload.length < 2 || payload.length % 2 !== 0) fail("invalid_frame", "payload");
  return payload;
}

export function packFrame(direction, sequence, timestampUs, payload, codec = CODEC_PCM_S16LE) {
  if (direction !== AUDIO_UPLINK && direction !== AUDIO_DOWNLINK) fail("invalid_frame", "direction");
  if (!Number.isInteger(codec)) fail("invalid_frame", "codec");
  if (codec !== CODEC_PCM_S16LE) fail("unsupported_codec", "codec");
  if (!Number.isInteger(sequence) || sequence < 0 || sequence > UINT32_MAX) {
    fail("invalid_frame", "sequence");
  }
  const timestamp = timestampBigInt(timestampUs);
  const body = asPayload(payload);
  const frame = new Uint8Array(HEADER_BYTES + body.length);
  const view = new DataView(frame.buffer);
  frame.set([0x41, 0x56, 0x49, 0x50], 0);
  view.setUint8(4, 1);
  view.setUint8(5, direction);
  view.setUint8(6, codec);
  view.setUint8(7, 0);
  view.setUint32(8, sequence, true);
  view.setBigUint64(12, timestamp, true);
  view.setUint32(20, body.length, true);
  frame.set(body, 24);
  return frame;
}

export function unpackFrame(buffer) {
  const bytes = buffer instanceof Uint8Array ? buffer : new Uint8Array(buffer);
  if (bytes.length > MAX_FRAME_BYTES) fail("frame_too_large", "payload");
  if (bytes.length < HEADER_BYTES) fail("invalid_frame", "header");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const magic = bytes[0] === 0x41 && bytes[1] === 0x56 && bytes[2] === 0x49 && bytes[3] === 0x50;
  if (!magic) fail("unsupported_protocol", "magic");
  if (view.getUint8(4) !== 1) fail("unsupported_protocol", "version");
  if (view.getUint8(7) !== 0) fail("invalid_frame", "reserved");
  const direction = view.getUint8(5);
  if (direction !== AUDIO_UPLINK && direction !== AUDIO_DOWNLINK) fail("invalid_frame", "direction");
  const codec = view.getUint8(6);
  if (codec !== CODEC_PCM_S16LE) fail("unsupported_codec", "codec");
  const sequence = view.getUint32(8, true);
  const timestampUs = view.getBigUint64(12, true);
  const payloadLength = view.getUint32(20, true);
  if (payloadLength > MAX_PAYLOAD_BYTES) fail("frame_too_large", "payload");
  const payload = bytes.slice(HEADER_BYTES);
  if (payloadLength !== payload.length) fail("invalid_frame", "payload");
  if (payload.length < 2 || payload.length % 2 !== 0) fail("invalid_frame", "payload");
  return {direction, codec, sequence, timestampUs, payload};
}

function canonicalCapabilities(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    fail("invalid_message", "capabilities");
  }
  const names = Object.keys(value);
  for (const name of names) {
    if (typeof value[name] !== "boolean") fail("invalid_message", "capabilities");
  }
  const ordered = {};
  for (const name of CAPABILITY_KEYS) {
    if (Object.prototype.hasOwnProperty.call(value, name)) ordered[name] = value[name];
  }
  const extras = names
    .filter((name) => !CAPABILITY_KEYS.includes(name))
    .sort((left, right) => (left < right ? -1 : left > right ? 1 : 0));
  for (const name of extras) ordered[name] = value[name];
  return ordered;
}

export function encodeSessionStart(message) {
  if (message === null || typeof message !== "object" || Array.isArray(message)) {
    fail("invalid_message");
  }
  const keys = Object.keys(message);
  if (keys.length !== ENVELOPE_KEYS.length || ENVELOPE_KEYS.some((key) => !keys.includes(key))) {
    fail("invalid_message");
  }
  if (message.protocol !== PROTOCOL) fail("unsupported_protocol", "protocol");
  if (message.type !== "session.start") fail("unsupported_message_type", "type");
  if (message.session_id !== null) fail("invalid_message", "session_id");
  if (typeof message.request_id !== "string" || !IDENTIFIER.test(message.request_id)) {
    fail("invalid_message", "request_id");
  }
  if (typeof message.device_id !== "string" || !IDENTIFIER.test(message.device_id)) {
    fail("invalid_message", "device_id");
  }
  return JSON.stringify({
    protocol: PROTOCOL,
    type: "session.start",
    request_id: message.request_id,
    session_id: null,
    device_id: message.device_id,
    capabilities: canonicalCapabilities(message.capabilities),
  });
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i += 1) {
    bytes[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

function bytesToHex(bytes) {
  return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
}

export class AvipBrowserClient {
  // `state` is "idle", "connecting", "open", or "closed". "closed" is terminal:
  // disconnect, a socket error, `close()`, or a full receive buffer all end
  // here. Every waiting `start` / `nextFrame` receiver rejects. Buffered
  // messages can still be read; the next receive after the buffer rejects.
  // This client does not reconnect. A new session starts at sequence 0.
  constructor({url, requestId, deviceId, capabilities}) {
    this.url = url;
    this.requestId = requestId;
    this.deviceId = deviceId;
    this.capabilities = capabilities;
    this.sequence = 0;
    this.timestampUs = 0n;
    this.socket = null;
    this.started = false;
    this.state = "idle";
    this.failure = null;
    this._pending = [];
    this._waiters = [];
    this._rejectConnect = null;
  }

  get pending() {
    return this._pending.length;
  }

  // `headers` is the Node WebSocket options bag (`new WebSocket(url, {headers})`).
  // Conformance passes Origin there because Node has no page. A browser
  // WebSocket's second argument is subprotocols, not headers, so a page calls
  // `connect()` with no argument. The browser sets Origin from the page
  // (`http://127.0.0.1:8000` or `http://localhost:8000`).
  connect(headers) {
    if (this.state !== "idle") fail("invalid_message");
    this.state = "connecting";
    this.socket = headers ? new WebSocket(this.url, {headers}) : new WebSocket(this.url);
    this.socket.binaryType = "arraybuffer";
    this.socket.addEventListener("message", (event) => {
      this._push(event.data);
    });
    this.socket.addEventListener("error", () => {
      this._fail(new Error("socket error"));
    });
    this.socket.addEventListener("close", () => {
      this._fail(new Error("socket closed"));
    });
    return new Promise((resolve, reject) => {
      let settled = false;
      this._rejectConnect = (error) => {
        if (settled) return;
        settled = true;
        reject(error);
      };
      this.socket.addEventListener("open", () => {
        if (settled) return;
        settled = true;
        this._rejectConnect = null;
        if (this.state === "closed") {
          reject(this.failure);
          return;
        }
        this.state = "open";
        resolve();
      });
    });
  }

  async start() {
    if (this.started) fail("session_already_started");
    if (this.state === "closed") throw this.failure;
    if (this.state !== "open") fail("invalid_message");
    const text = encodeSessionStart({
      protocol: PROTOCOL,
      type: "session.start",
      request_id: this.requestId,
      session_id: null,
      device_id: this.deviceId,
      capabilities: this.capabilities,
    });
    this.socket.send(text);
    const message = await this._nextJson();
    if (message.type !== "session.ready") fail("invalid_message", "type");
    this.started = true;
    return message;
  }

  // The clock advances only after `send` returns. A throw means the socket
  // did not accept the frame, so the same sequence may be retried on this
  // socket while it is still open. A return means the bytes were queued
  // locally, not that the peer acknowledged them. If the socket later closes,
  // that frame may or may not have been delivered; do not send that sequence
  // again here. A new connection is a new session and starts at sequence 0.
  sendUplink(payload) {
    if (this.state === "closed") throw this.failure;
    if (!this.started || this.state !== "open") fail("invalid_message");
    const frame = packFrame(AUDIO_UPLINK, this.sequence, this.timestampUs, payload);
    this.socket.send(frame);
    this.sequence = (this.sequence + 1) >>> 0;
    this.timestampUs += NOMINAL_CHUNK_US;
    return frame;
  }

  // Advance the media clock for captured audio that was not sent. Sequence
  // stays put, and no silence frame is written. The next sendUplink timestamp
  // includes the gap, so a contiguous sequence is not continuous speech.
  noteCaptureGap(durationUs) {
    if (this.state === "closed") throw this.failure;
    if (!this.started || this.state !== "open") fail("invalid_message");
    const gap = timestampBigInt(durationUs);
    if (gap === 0n) return;
    const next = this.timestampUs + gap;
    if (next > UINT64_MAX) fail("invalid_frame", "timestamp_us");
    this.timestampUs = next;
  }

  async nextFrame() {
    const data = await this._next();
    if (typeof data === "string") {
      const message = JSON.parse(data);
      if (message.type === "error") fail(message.code, message.field);
      return message;
    }
    const unpacked = unpackFrame(new Uint8Array(data));
    unpacked.raw = new Uint8Array(data);
    return unpacked;
  }

  close() {
    if (this.state === "idle") {
      this.state = "closed";
      this.failure = new Error("socket closed");
      return;
    }
    if (this.state !== "closed") this._fail(new Error("socket closed"));
    this._closeSocket();
  }

  _fail(error) {
    if (this.state === "closed") return;
    const connecting = this.state === "connecting";
    this.state = "closed";
    this.failure = error;
    if (connecting && this._rejectConnect) {
      const rejectConnect = this._rejectConnect;
      this._rejectConnect = null;
      rejectConnect(error);
    } else {
      this._rejectConnect = null;
    }
    const waiters = this._waiters;
    this._waiters = [];
    for (const waiter of waiters) waiter.reject(error);
  }

  _closeSocket() {
    const socket = this.socket;
    if (!socket || socket.readyState >= 2) return;
    try {
      socket.close();
    } catch {
      // The socket is already closing.
    }
  }

  _push(value) {
    if (this.state === "closed") return;
    const waiter = this._waiters.shift();
    if (waiter) {
      waiter.resolve(value);
      return;
    }
    if (this._pending.length >= MAX_PENDING_MESSAGES) {
      this._fail(new Error("queue_full"));
      this._closeSocket();
      return;
    }
    this._pending.push(value);
  }

  _next() {
    if (this._pending.length > 0) return Promise.resolve(this._pending.shift());
    if (this.state === "closed") return Promise.reject(this.failure);
    return new Promise((resolve, reject) => {
      this._waiters.push({resolve, reject});
    });
  }

  async _nextJson() {
    const data = await this._next();
    if (typeof data !== "string") fail("invalid_message");
    const message = JSON.parse(data);
    if (message.type === "error") fail(message.code, message.field);
    return message;
  }
}

export {AUDIO_DOWNLINK, AUDIO_UPLINK, MAX_PENDING_MESSAGES, bytesToHex, hexToBytes};
