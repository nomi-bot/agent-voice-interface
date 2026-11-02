// Browser microphone and speaker path for the existing AVIP client.
// Uplink frames are the M0 profile already packed by avip-client.js:
// 16 kHz mono pcm_s16le, 20 ms (640 bytes). Downlink frames are the same
// profile. Nothing here speaks a second framing protocol.
//
// stopPlayback flushes only the local speaker queue. It does not write a
// control envelope, does not close the socket, and does not cancel remote
// agent work. This client has no agent runtime and no physical actuation.

import {AUDIO_DOWNLINK} from "./avip-client.js";
import {
  AVIP_HEADER_BYTES,
  FRAME_BYTES,
  FRAME_SAMPLES,
  FRAME_US,
  FrameChunker,
  GEMINI_LIVE_OUTPUT_RATE_HZ,
  MAX_BUFFER_FRAMES,
  SAMPLE_RATE_HZ,
  s16leToFloat,
} from "./pcm.js";
import {VOICE_MARKS, VoiceTelemetry} from "./telemetry.js";

// The page and the checks still import this name. session-start is the hook
// added for the measurement report. The other four are the JEV-24 names.
export const VOICE_METRICS = VOICE_MARKS;

// One capture callback keeps the newest frames and counts the rest as drops.
// The depth is the same 8-frame cap as MediaDelivery and the speaker buffer.
export function trimCaptureBurst(frames, depth = MAX_BUFFER_FRAMES) {
  if (!Array.isArray(frames)) throw new Error("invalid_audio");
  if (frames.length <= depth) return {frames, dropped: 0};
  const dropped = frames.length - depth;
  return {frames: frames.slice(dropped), dropped};
}

export const DEVELOPMENT_ORIGINS = ["http://127.0.0.1:8000", "http://localhost:8000"];
export const CAPTURE_WORKLET_URL = "/voice/capture-worklet.js";
export const PLAYBACK_GAIN = 0.6;
// One nominal uplink frame on the wire, header included. The browser
// WebSocket queues this many bytes in bufferedAmount, not the PCM alone.
export const UPLINK_FRAME_BYTES = AVIP_HEADER_BYTES + FRAME_BYTES;
// Same depth as MediaDelivery: 8 nominal frames. Past this, new uplink PCM
// is dropped and the sequence does not advance.
export const MAX_UPLINK_QUEUED_BYTES = MAX_BUFFER_FRAMES * UPLINK_FRAME_BYTES;
// A queue already twice that deep is not a live mic anymore. Close it.
export const UPLINK_CLOSE_QUEUED_BYTES = MAX_UPLINK_QUEUED_BYTES * 2;
const NOMINAL_FRAME_SEC = FRAME_SAMPLES / SAMPLE_RATE_HZ;
const SECURE_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);

const MIC_CONSTRAINTS = {
  audio: {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
  video: false,
};

export function voiceError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

// "send" queues one frame. "drop" skips it and leaves the uplink sequence
// where it is. "close" means the socket has no usable byte budget.
export function uplinkQueueDecision(bufferedAmount, frameBytes = UPLINK_FRAME_BYTES) {
  if (typeof bufferedAmount !== "number" || !Number.isFinite(bufferedAmount) || bufferedAmount < 0) {
    return "close";
  }
  if (bufferedAmount >= UPLINK_CLOSE_QUEUED_BYTES) return "close";
  if (bufferedAmount + frameBytes > MAX_UPLINK_QUEUED_BYTES) return "drop";
  return "send";
}

// AVIP version 1 downlink is 16 kHz. Passing any other rate throws instead of
// scheduling those bytes on a 16 kHz buffer. Gemini Live output is 24 kHz and
// has to go through resampleS16leToAvip before it is an AVIP payload.
export function playbackSchedule(payload, sourceRateHz) {
  if (sourceRateHz !== SAMPLE_RATE_HZ) {
    throw voiceError(
      "sample_rate_mismatch",
      `Refusing to play ${sourceRateHz} Hz PCM as AVIP ${SAMPLE_RATE_HZ} Hz. ` +
      `Gemini Live output is ${GEMINI_LIVE_OUTPUT_RATE_HZ} Hz and must be resampled to ` +
      `${SAMPLE_RATE_HZ} Hz before it is packed as an AVIP downlink frame.`,
    );
  }
  const bytes = payload instanceof Uint8Array ? payload : new Uint8Array(payload);
  if (bytes.length % 2 !== 0) throw voiceError("sample_rate_mismatch", "AVIP playback expects pcm_s16le bytes.");
  return {
    sampleRateHz: SAMPLE_RATE_HZ,
    durationSec: (bytes.length / 2) / SAMPLE_RATE_HZ,
    payload: bytes,
  };
}

export function pageOrigin(page) {
  if (!page || !page.protocol || !page.hostname) return "";
  const port = page.port ? `:${page.port}` : "";
  return `${page.protocol}//${page.hostname}${port}`;
}

export function secureContext(page) {
  const protocol = page?.protocol || "";
  const hostname = (page?.hostname || "").toLowerCase();
  const secure = protocol === "https:" || (protocol === "http:" && SECURE_HOSTS.has(hostname));
  if (secure) return {ok: true, origin: pageOrigin(page)};
  return {
    ok: false,
    code: "insecure_origin",
    origin: pageOrigin(page),
    message:
      "Microphone capture needs a secure context: HTTPS, or http://localhost, or http://127.0.0.1. " +
      "A file:// page and other plain http hosts cannot use the microphone.",
  };
}

export function developmentGatewayOrigin(page) {
  return DEVELOPMENT_ORIGINS.includes(pageOrigin(page));
}

function defaultNow() {
  if (typeof performance !== "undefined" && typeof performance.now === "function") return performance.now();
  return Date.now();
}

export function browserMedia() {
  return {
    location() {
      if (typeof location === "undefined") return {protocol: "", hostname: "", port: ""};
      return location;
    },
    getUserMedia(constraints) {
      const devices = typeof navigator === "undefined" ? undefined : navigator.mediaDevices;
      if (!devices || typeof devices.getUserMedia !== "function") {
        const error = new Error(
          "getUserMedia is not available. Open this page on HTTPS or on http://localhost / http://127.0.0.1.",
        );
        error.name = "SecurityError";
        throw error;
      }
      return devices.getUserMedia(constraints);
    },
  };
}

export class SpeakerBuffer {
  // Schedules payload bytes through `play`. Drops a new frame when the
  // scheduled horizon is already MAX_BUFFER_FRAMES nominal chunks ahead.
  // That matches the gateway MediaDelivery depth. stop() cancels every
  // local handle and does not call back into a socket.
  constructor({now, play, depth = MAX_BUFFER_FRAMES}) {
    if (typeof now !== "function" || typeof play !== "function") throw new Error("invalid_audio");
    this.now = now;
    this.play = play;
    this.depth = depth;
    this.handles = [];
    this.nextTime = 0;
    this.played = 0;
    this.dropped = 0;
  }

  enqueue(payload) {
    this.handles = this.handles.filter((handle) => !handle.ended);
    const start = this.now();
    if (this.nextTime < start) this.nextTime = start;
    if (this.nextTime - start >= this.depth * NOMINAL_FRAME_SEC) {
      this.dropped += 1;
      return false;
    }
    const handle = this.play(payload, this.nextTime) || {stop() {}};
    this.handles.push(handle);
    const samples = payload.length / 2;
    this.nextTime += samples / SAMPLE_RATE_HZ;
    this.played += 1;
    return true;
  }

  // Scheduled horizon in nominal 20 ms frames, on the audio clock passed as
  // `now` (AudioContext.currentTime in the page). This is not the page clock.
  depthFrames() {
    const start = this.now();
    const horizon = this.nextTime - start;
    if (horizon <= 0) return 0;
    return horizon / NOMINAL_FRAME_SEC;
  }

  stop() {
    for (const handle of this.handles) {
      try {
        handle.stop();
      } catch {
        // The buffer source already ended.
      }
    }
    this.handles = [];
    this.nextTime = 0;
  }
}

function copyToBuffer(buffer, samples) {
  if (typeof buffer.copyToChannel === "function") {
    buffer.copyToChannel(samples, 0);
    return;
  }
  buffer.getChannelData(0).set(samples);
}

export function createBrowserAudio() {
  const Context = typeof AudioContext !== "undefined"
    ? AudioContext
    : typeof webkitAudioContext !== "undefined"
      ? webkitAudioContext
      : null;
  let context = null;
  let streamSource = null;
  let captureNode = null;
  let mute = null;
  let gain = null;

  function disconnect(node) {
    if (!node) return;
    try {
      node.disconnect();
    } catch {
      // Already disconnected.
    }
  }

  return {
    begin() {
      if (!Context) throw voiceError("audio_unavailable", "Web Audio is not available in this browser.");
      if (!context || context.state === "closed") context = new Context({sampleRate: SAMPLE_RATE_HZ});
      return context;
    },
    async open({stream, onSamples}) {
      const ctx = this.begin();
      if (ctx.state === "suspended") await ctx.resume();
      streamSource = ctx.createMediaStreamSource(stream);
      let usedWorklet = false;
      mute = ctx.createGain();
      mute.gain.value = 0;
      mute.connect(ctx.destination);
      if (ctx.audioWorklet && typeof AudioWorkletNode !== "undefined") {
        try {
          await ctx.audioWorklet.addModule(CAPTURE_WORKLET_URL);
          const node = new AudioWorkletNode(ctx, "avip-capture");
          node.port.onmessage = (event) => {
            if (event.data instanceof Float32Array) onSamples(event.data, ctx.sampleRate);
          };
          streamSource.connect(node);
          // The worklet has to stay in the graph or the browser stops pulling it.
          // The gain is zero so the microphone is not monitored through the speakers.
          node.connect(mute);
          captureNode = node;
          usedWorklet = true;
        } catch {
          usedWorklet = false;
        }
      }
      if (!usedWorklet) {
        if (typeof ctx.createScriptProcessor !== "function") {
          throw voiceError("audio_unavailable", "This browser has no microphone processor.");
        }
        const processor = ctx.createScriptProcessor(1024, 1, 1);
        processor.onaudioprocess = (event) => {
          onSamples(new Float32Array(event.inputBuffer.getChannelData(0)), ctx.sampleRate);
        };
        streamSource.connect(processor);
        processor.connect(mute);
        captureNode = processor;
      }
      gain = ctx.createGain();
      gain.gain.value = PLAYBACK_GAIN;
      gain.connect(ctx.destination);
      const speaker = new SpeakerBuffer({
        now: () => ctx.currentTime,
        play(payload, when) {
          const scheduled = playbackSchedule(payload, SAMPLE_RATE_HZ);
          const floats = s16leToFloat(scheduled.payload);
          const buffer = ctx.createBuffer(1, floats.length, scheduled.sampleRateHz);
          copyToBuffer(buffer, floats);
          const node = ctx.createBufferSource();
          node.buffer = buffer;
          node.connect(gain);
          node.start(when);
          const handle = {
            ended: false,
            stop() {
              try {
                node.stop();
              } catch {
                // Already stopped.
              }
            },
          };
          node.onended = () => {
            handle.ended = true;
          };
          return handle;
        },
      });
      return {speaker, sampleRate: ctx.sampleRate};
    },
    async close() {
      disconnect(streamSource);
      disconnect(captureNode);
      disconnect(mute);
      disconnect(gain);
      streamSource = null;
      captureNode = null;
      mute = null;
      gain = null;
      const ctx = context;
      context = null;
      if (ctx && ctx.state !== "closed") await ctx.close();
    },
  };
}

function denied(error) {
  const name = error && error.name;
  return name === "NotAllowedError" || name === "PermissionDeniedError";
}

export class BrowserVoice {
  constructor({
    now = defaultNow,
    wallNow,
    onMetric = () => {},
    onStatus = () => {},
    onStats = () => {},
    media = browserMedia(),
    audio = createBrowserAudio(),
    downlinkRateHz = SAMPLE_RATE_HZ,
    path = "mock-mirror",
  } = {}) {
    this.now = now;
    this.onMetric = onMetric;
    this.onStatus = onStatus;
    this.onStats = onStats;
    this.media = media;
    this.audio = audio;
    this.downlinkRateHz = downlinkRateHz;
    this.telemetry = new VoiceTelemetry({
      now: this.now,
      wallNow,
      client: "browser",
      path,
      scenario: "page",
    });
    this.metrics = {};
    this.stats = {
      uplinkFrames: 0,
      downlinkFrames: 0,
      playbackFrames: 0,
      captureDrops: 0,
      playbackDrops: 0,
      transmitDrops: 0,
      skippedMediaUs: 0,
    };
    this.client = null;
    this.stream = null;
    this.speaker = null;
    this.chunker = new FrameChunker();
    this.running = false;
    this.captureLive = false;
    this.playbackHalted = false;
    this._closing = false;
    this._shutdownDone = false;
    this._reader = null;
  }

  _mark(name) {
    this._markAt(name, this.now());
  }

  // `clock: "local"` is the session clock on this page (performance.now, or
  // the injected now). It is not the wall clock and not the AVIP media clock.
  _markAt(name, at) {
    if (Object.prototype.hasOwnProperty.call(this.metrics, name)) return;
    this.metrics[name] = at;
    this.telemetry.mark(name, at);
    this.onMetric(name, {at, clock: "local"});
  }

  _touchStats() {
    this.telemetry.setCounters({
      uplinkFrames: this.stats.uplinkFrames,
      downlinkFrames: this.stats.downlinkFrames,
      playbackFrames: this.stats.playbackFrames,
      captureDrops: this.stats.captureDrops,
      transmitDrops: this.stats.transmitDrops,
      speakerDrops: this.stats.playbackDrops,
      timeouts: this.telemetry.counters.timeouts,
      queueFull: this.telemetry.counters.queueFull,
    });
    this.onStats(this.stats);
  }

  _releaseStream() {
    this.captureLive = false;
    const stream = this.stream;
    this.stream = null;
    if (!stream) return;
    for (const track of stream.getTracks()) {
      try {
        track.stop();
      } catch {
        // The track has already ended.
      }
    }
  }

  async _shutdown(status, detail) {
    if (this._shutdownDone) return;
    this._shutdownDone = true;
    this._closing = true;
    this.running = false;
    this.playbackHalted = true;
    this._releaseStream();
    if (this.speaker) this.speaker.stop();
    this.speaker = null;
    await this.audio.close();
    if (status) this.onStatus(status, detail || {});
    this._closeClient();
  }

  _adopt(client) {
    if (!client || typeof client.close !== "function") {
      throw voiceError("invalid_state", "A voice client must have close().");
    }
    if (this.client && this.client !== client) {
      try {
        this.client.close();
      } catch {
        // The previous client is already closing.
      }
    }
    this.client = client;
  }

  _closeClient() {
    const client = this.client;
    if (!client || client.state === "closed" || typeof client.close !== "function") return;
    try {
      client.close();
    } catch {
      // The socket is already closing.
    }
  }

  // `connect(lease)` owns the socket from the moment it can fail.
  // Call `lease.adopt(client)` as soon as the WebSocket exists, before
  // handshake or capability checks. BrowserVoice then closes that client if
  // `connect` throws or negotiation fails, and it stops the microphone.
  // Returning `{client, ready}` also transfers ownership. A connector that
  // opens a socket and throws without adopt leaves a socket this object
  // cannot see.
  // The microphone is acquired before `connect` so a refused permission
  // never opens a socket.
  async start({connect} = {}) {
    if (this.running || this._shutdownDone) throw voiceError("invalid_state", "Voice session already started.");
    if (typeof connect !== "function") throw voiceError("invalid_state", "connect is required.");
    const gate = secureContext(this.media.location());
    if (!gate.ok) {
      this.onStatus("insecure-origin", gate);
      throw voiceError("insecure_origin", gate.message);
    }
    this.audio.begin();
    this.onStatus("requesting-permission", {});
    let stream;
    try {
      stream = await this.media.getUserMedia(MIC_CONSTRAINTS);
    } catch (error) {
      await this.audio.close();
      if (error && error.name === "SecurityError") {
        this.onStatus("insecure-origin", {message: error.message});
        throw voiceError("insecure_origin", error.message);
      }
      if (denied(error)) {
        this.onStatus("permission-denied", {
          message: "Microphone permission was denied. Allow the microphone for this page and press Start again.",
        });
        throw voiceError(
          "permission_denied",
          "Microphone permission was denied. Allow the microphone for this page and press Start again.",
        );
      }
      this.onStatus("capture-failed", {message: error && error.message, name: error && error.name});
      throw error;
    }
    const tracks = stream.getAudioTracks();
    if (!tracks.length) {
      this.stream = stream;
      this._releaseStream();
      await this.audio.close();
      this.onStatus("device-ended", {message: "No microphone track was opened."});
      throw voiceError("device_ended", "No microphone track was opened.");
    }
    this.stream = stream;
    const track = tracks[0];
    track.addEventListener("ended", () => {
      if (this._closing || !this.captureLive) return;
      this._onDeviceEnded();
    });
    this.captureLive = true;
    this._mark("capture-start");
    this.onStatus("connecting", {});
    const lease = {adopt: (client) => this._adopt(client)};
    try {
      const opened = await connect(lease);
      if (opened && opened.client) this._adopt(opened.client);
      if (!this.client) throw voiceError("invalid_state", "connect returned no client");
      if (!this.captureLive) {
        throw voiceError("device_ended", "The microphone ended before the session was ready.");
      }
      const capabilities = opened.ready && opened.ready.capabilities;
      if (!capabilities || !capabilities.audio_input || !capabilities.audio_output) {
        throw voiceError(
          "audio_unavailable",
          "The gateway did not negotiate audio. Start it with AVIP_MOCK_MEDIA=1.",
        );
      }
      if (capabilities.robot_actions) {
        throw voiceError("invalid_state", "This voice client does not enable robot actions.");
      }
      const audio = await this.audio.open({
        stream: this.stream,
        onSamples: (samples, sampleRate) => this._onSamples(samples, sampleRate),
      });
      this.speaker = audio.speaker;
    } catch (error) {
      this.captureLive = false;
      this._releaseStream();
      await this.audio.close();
      this._closeClient();
      if (error && error.code === "audio_unavailable") {
        this.onStatus("audio-unavailable", {message: error.message});
      } else if (error && error.code === "device_ended") {
        this.onStatus("device-ended", {message: error.message});
      } else {
        this.onStatus("disconnected", {message: error && error.message});
      }
      throw error;
    }
    this.running = true;
    this._mark("session-start");
    this._reader = this._readLoop();
    this.onStatus("live", {});
  }

  _onDeviceEnded() {
    this.captureLive = false;
    this._releaseStream();
    this.chunker.reset();
    this.onStatus("device-ended", {
      message: "The microphone ended. Capture stopped. The session is still open, and no cancel was sent.",
    });
  }

  _bufferedAmount() {
    const socket = this.client && this.client.socket;
    if (!socket) return null;
    return socket.bufferedAmount;
  }

  _onSamples(samples, sampleRate) {
    if (!this.captureLive || !this.client || this.client.state === "closed") return;
    let frames;
    try {
      frames = this.chunker.push(samples, sampleRate);
    } catch (error) {
      this.onStatus("capture-failed", {message: error.message});
      return;
    }
    const trimmed = trimCaptureBurst(frames);
    if (trimmed.dropped) {
      this.stats.captureDrops += trimmed.dropped;
      this._skipMedia(trimmed.dropped);
      frames = trimmed.frames;
    }
    let sent = 0;
    let dropped = 0;
    for (let index = 0; index < frames.length; index += 1) {
      const payload = frames[index];
      if (!this.captureLive || this.client.state === "closed") break;
      const buffered = this._bufferedAmount();
      this.telemetry.noteUplinkQueued(buffered);
      const decision = uplinkQueueDecision(buffered, AVIP_HEADER_BYTES + payload.length);
      if (decision === "send") {
        try {
          this.client.sendUplink(payload);
        } catch (error) {
          this._touchStats();
          void this._shutdown("disconnected", {message: error.message}).catch(() => {});
          return;
        }
        const at = this.now();
        this.stats.uplinkFrames += 1;
        this.telemetry.noteUplink(at);
        this._markAt("first-uplink", at);
        sent += 1;
        continue;
      }
      const unused = frames.length - index;
      this.stats.transmitDrops += unused;
      this._skipMedia(unused);
      dropped += unused;
      if (decision === "close") {
        this.telemetry.noteTimeout("uplink-stalled");
        void this._shutdown("uplink-stalled", {
          message: "The uplink send queue stopped draining. Capture stopped and the session was closed. No cancel was sent.",
        }).catch(() => {});
      }
      break;
    }
    if (sent || dropped || trimmed.dropped) this._touchStats();
  }

  // Dropped capture is not a sent frame. Move the uplink media clock forward
  // by the nominal duration and leave the sequence where it is. Silence is
  // not inserted: the send queue is already the reason the frame was skipped.
  _skipMedia(frameCount) {
    if (!frameCount) return;
    const gap = FRAME_US * frameCount;
    this.stats.skippedMediaUs += gap;
    const client = this.client;
    if (client && typeof client.noteCaptureGap === "function" && client.state !== "closed") {
      client.noteCaptureGap(gap);
    }
  }

  _onDownlink(payload) {
    const at = this.now();
    this.stats.downlinkFrames += 1;
    this._markAt("first-downlink", at);
    this.telemetry.noteDownlink(at);
    let scheduled;
    try {
      scheduled = playbackSchedule(payload, this.downlinkRateHz);
    } catch (error) {
      this.stats.playbackDrops += 1;
      this.onStatus("playback-failed", {message: error.message});
      this._touchStats();
      return;
    }
    if (this.playbackHalted || !this.speaker) {
      this._touchStats();
      return;
    }
    const played = this.speaker.enqueue(scheduled.payload);
    if (typeof this.speaker.depthFrames === "function") {
      this.telemetry.noteBufferDepth(this.speaker.depthFrames());
    }
    if (played) {
      const heard = this.now();
      this.stats.playbackFrames += 1;
      this._markAt("playback-start", heard);
      this.telemetry.noteAudible(heard);
    } else {
      this.stats.playbackDrops += 1;
    }
    this._touchStats();
  }

  async _readLoop() {
    while (this.running) {
      let message;
      try {
        message = await this.client.nextFrame();
      } catch (error) {
        if (this._shutdownDone || !this.running) return;
        if (this.client.state !== "closed") {
          const message = error && error.message ? String(error.message) : "";
          if (message === "queue_full" || message.startsWith("queue_full:")) {
            this.telemetry.noteQueueFull();
            this._touchStats();
          }
          this.onStatus("avip-error", {message: error.message});
          continue;
        }
        await this._shutdown("disconnected", {message: error.message});
        return;
      }
      if (!this.running) return;
      try {
        if (message && message.direction === AUDIO_DOWNLINK && message.payload) this._onDownlink(message.payload);
      } catch (error) {
        this.stopPlayback();
        this.onStatus("playback-failed", {message: error.message});
      }
    }
  }

  stopPlayback() {
    this.playbackHalted = true;
    if (this.speaker) this.speaker.stop();
    this.onStatus("playback-stopped", {
      scope: "local",
      message: "Speaker playback stopped on this page. The session is still open, and no cancel was sent.",
    });
  }

  async stop() {
    await this._shutdown("ended", {message: "Session closed on this page."});
    if (this._reader) await this._reader;
  }
}
