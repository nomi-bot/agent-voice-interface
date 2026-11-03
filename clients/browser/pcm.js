// PCM helpers for the M0 AVIP profile: 16 kHz mono pcm_s16le, 20 ms frames.
// This file does not open devices or sockets. The browser voice path and the
// Node checks both use it. Frame bytes are handed to avip-client.js packFrame.

export const SAMPLE_RATE_HZ = 16000;
// Gemini Live's published output rate. It is not the AVIP v1 profile. A
// provider must resample this to SAMPLE_RATE_HZ before packing a downlink
// frame. This module does not connect to Gemini.
export const GEMINI_LIVE_OUTPUT_RATE_HZ = 24000;
export const FRAME_US = 20000;
export const FRAME_SAMPLES = SAMPLE_RATE_HZ * FRAME_US / 1_000_000;
export const FRAME_BYTES = FRAME_SAMPLES * 2;
export const AVIP_HEADER_BYTES = 24;
// MediaDelivery keeps at most 8 frames per direction (160 ms at 20 ms).
export const MAX_BUFFER_FRAMES = 8;
const INT16_MAX = 32767;
const INT16_MIN = -32768;

function asFloat32(samples) {
  if (samples instanceof Float32Array) return samples;
  if (ArrayBuffer.isView(samples) || Array.isArray(samples)) return Float32Array.from(samples);
  throw new Error("invalid_audio");
}

export function floatToS16le(samples) {
  const input = asFloat32(samples);
  const out = new Uint8Array(input.length * 2);
  const view = new DataView(out.buffer);
  for (let i = 0; i < input.length; i += 1) {
    const clamped = Math.max(-1, Math.min(1, input[i]));
    const value = clamped < 0 ? Math.round(clamped * 32768) : Math.round(clamped * INT16_MAX);
    view.setInt16(i * 2, Math.max(INT16_MIN, Math.min(INT16_MAX, value)), true);
  }
  return out;
}

export function s16leToFloat(payload) {
  const bytes = payload instanceof Uint8Array ? payload : new Uint8Array(payload);
  if (bytes.length % 2 !== 0) throw new Error("invalid_frame: payload");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const out = new Float32Array(bytes.length / 2);
  for (let i = 0; i < out.length; i += 1) {
    const value = view.getInt16(i * 2, true);
    out[i] = value < 0 ? value / 32768 : value / INT16_MAX;
  }
  return out;
}

function concatFloat(left, right) {
  if (left.length === 0) return right instanceof Float32Array ? new Float32Array(right) : Float32Array.from(right);
  const out = new Float32Array(left.length + right.length);
  out.set(left, 0);
  out.set(right, left.length);
  return out;
}

// Streaming linear resampler. The read cursor carries across pushes so a
// 48 kHz render quantum becomes a continuous 16 kHz stream, not a series of
// independently rounded blocks.
class RateConverter {
  constructor(fromRate, toRate) {
    if (!Number.isFinite(fromRate) || !Number.isFinite(toRate) || fromRate <= 0 || toRate <= 0) {
      throw new Error("invalid_sample_rate");
    }
    this.fromRate = fromRate;
    this.toRate = toRate;
    this.step = fromRate / toRate;
    this.input = new Float32Array(0);
    this.pos = 0;
  }

  push(samples) {
    const chunk = asFloat32(samples);
    this.input = concatFloat(this.input, chunk);
    const produced = [];
    while (this.pos + 1 < this.input.length) {
      const index = Math.floor(this.pos);
      const fraction = this.pos - index;
      const a = this.input[index];
      const b = this.input[index + 1];
      produced.push(a + (b - a) * fraction);
      this.pos += this.step;
    }
    const drop = Math.floor(this.pos);
    if (drop > 0) {
      this.input = this.input.subarray(drop);
      this.pos -= drop;
    }
    return Float32Array.from(produced);
  }
}

export class FrameChunker {
  constructor() {
    this.pending = new Float32Array(0);
    this.sampleRate = null;
    this._converter = null;
  }

  reset() {
    this.pending = new Float32Array(0);
    this.sampleRate = null;
    this._converter = null;
  }

  push(samples, sampleRate) {
    if (!Number.isFinite(sampleRate) || sampleRate <= 0) throw new Error("invalid_sample_rate");
    if (this.sampleRate === null) this.sampleRate = sampleRate;
    if (this.sampleRate !== sampleRate) throw new Error("sample_rate_changed");
    const incoming = asFloat32(samples);
    const atRate = sampleRate === SAMPLE_RATE_HZ ? new Float32Array(incoming) : this._resample(incoming);
    this.pending = concatFloat(this.pending, atRate);
    const frames = [];
    while (this.pending.length >= FRAME_SAMPLES) {
      frames.push(floatToS16le(this.pending.subarray(0, FRAME_SAMPLES)));
      this.pending = this.pending.slice(FRAME_SAMPLES);
    }
    return frames;
  }

  _resample(samples) {
    if (!this._converter) this._converter = new RateConverter(this.sampleRate, SAMPLE_RATE_HZ);
    return this._converter.push(samples);
  }
}

// One-shot conversion onto the AVIP clock. The result is pcm_s16le at
// 16 kHz. It is not framed and it is not a second wire protocol.
export function resampleFloat(samples, fromRate, toRate) {
  if (!Number.isFinite(fromRate) || !Number.isFinite(toRate) || fromRate <= 0 || toRate <= 0) {
    throw new Error("invalid_sample_rate");
  }
  const input = asFloat32(samples);
  if (fromRate === toRate) return new Float32Array(input);
  return new RateConverter(fromRate, toRate).push(input);
}

export function resampleS16leToAvip(payload, sourceRateHz) {
  const bytes = payload instanceof Uint8Array ? payload : new Uint8Array(payload);
  if (sourceRateHz === SAMPLE_RATE_HZ) return new Uint8Array(bytes);
  return floatToS16le(resampleFloat(s16leToFloat(bytes), sourceRateHz, SAMPLE_RATE_HZ));
}

// Persistent converter for a sequence of PCM chunks. A fresh
// resampleS16leToAvip call starts a new cursor; this object keeps it.
export class S16leResampler {
  constructor(fromRate, toRate) {
    if (!Number.isFinite(fromRate) || !Number.isFinite(toRate) || fromRate <= 0 || toRate <= 0) {
      throw new Error("invalid_sample_rate");
    }
    this.fromRate = fromRate;
    this.toRate = toRate;
    this._converter = fromRate === toRate ? null : new RateConverter(fromRate, toRate);
  }

  push(payload) {
    const bytes = payload instanceof Uint8Array ? payload : new Uint8Array(payload);
    if (bytes.length % 2 !== 0) throw new Error("invalid_frame: payload");
    if (!this._converter) return new Uint8Array(bytes);
    return floatToS16le(this._converter.push(s16leToFloat(bytes)));
  }
}
