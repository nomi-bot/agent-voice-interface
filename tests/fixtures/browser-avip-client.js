// Browser-shaped AVIP client for http://127.0.0.1:8000 or http://localhost:8000.
// Paste this file into that page's developer console. The gateway must already
// be running. Set AVIP_MOCK_MEDIA=1 before starting it to negotiate the M0 PCM
// mirror; otherwise session.ready reports every capability false and a binary
// frame closes the socket. This file does not import the Python framing module.
// Its bytes follow tests/fixtures/avip-v1-media.json.

const GOLDEN_UPLINK_HEX = "41564950010101000000000000000000000000000400000000000000";

function packFrame(direction, sequence, timestampUs, payload) {
  const frame = new Uint8Array(24 + payload.length);
  const view = new DataView(frame.buffer);
  frame.set([0x41, 0x56, 0x49, 0x50], 0);
  view.setUint8(4, 1);
  view.setUint8(5, direction);
  view.setUint8(6, 1);
  view.setUint8(7, 0);
  view.setUint32(8, sequence, true);
  view.setBigUint64(12, BigInt(timestampUs), true);
  view.setUint32(20, payload.length, true);
  frame.set(payload, 24);
  return frame.buffer;
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i += 1) {
    bytes[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

function bytesToHex(buffer) {
  return [...new Uint8Array(buffer)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const avip = new WebSocket(`ws://${location.host}/ws/avip`);
avip.binaryType = "arraybuffer";
avip.onmessage = (event) => {
  if (typeof event.data === "string") {
    const message = JSON.parse(event.data);
    console.log(message);
    if (message.type === "session.ready" && message.capabilities.audio_input && message.capabilities.audio_output) {
      avip.send(hexToBytes(GOLDEN_UPLINK_HEX));
    }
    return;
  }
  console.log("downlink", bytesToHex(event.data));
};
avip.onopen = () => avip.send(JSON.stringify({
  protocol: "avip/1",
  type: "session.start",
  request_id: "req-browser-001",
  session_id: null,
  device_id: "browser-demo-001",
  capabilities: {audio_input: true, audio_output: true, camera: false, display: false},
}));
