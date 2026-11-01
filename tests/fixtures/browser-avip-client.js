// Browser demo for http://127.0.0.1:8000 or http://localhost:8000.
// Paste this file into that page's developer console. The gateway must already
// be running. Set AVIP_MOCK_MEDIA=1 before starting it to negotiate the M0 PCM
// mirror; otherwise session.ready reports every capability false and a binary
// frame closes the socket.
//
// This paste sends one golden uplink frame. It does not encode frames and it
// does not open the microphone. The browser adapter is
// clients/browser/avip-client.js. The microphone and speaker page is
// http://127.0.0.1:8000/voice when the gateway is started with AVIP_MOCK_MEDIA=1.

const GOLDEN_UPLINK_HEX = "41564950010101000000000000000000000000000400000000000000";

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
