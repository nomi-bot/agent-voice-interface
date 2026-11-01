// Conformance driver for avip-client.js. The Python tests spawn this file.
// `encode` reads JSON from stdin and writes packed frames and handshake text.
// `roundtrip` opens /ws/avip, sends one uplink payload, and prints the downlink.

import {readFileSync} from "node:fs";

import {
  AvipBrowserClient,
  bytesToHex,
  encodeSessionStart,
  hexToBytes,
  packFrame,
  unpackFrame,
} from "./avip-client.js";

const command = process.argv[2];

if (command === "encode") {
  const request = JSON.parse(readFileSync(0, "utf8"));
  const frames = request.frames.map((frame) => {
    const payload = hexToBytes(frame.payload_hex);
    const packed = packFrame(
      frame.direction,
      frame.sequence,
      BigInt(frame.timestamp_us),
      payload,
    );
    const decoded = unpackFrame(packed);
    return {
      frame_hex: bytesToHex(packed),
      direction: decoded.direction,
      sequence: decoded.sequence,
      timestamp_us: decoded.timestampUs.toString(),
      payload_hex: bytesToHex(decoded.payload),
    };
  });
  const handshakes = request.handshakes.map((message) => encodeSessionStart(message));
  const rejected = (request.rejected || []).map((message) => {
    try {
      encodeSessionStart(message);
      return null;
    } catch (error) {
      return error.message;
    }
  });
  process.stdout.write(JSON.stringify({frames, handshakes, rejected}));
} else if (command === "roundtrip") {
  const url = process.argv[3];
  const payload = hexToBytes(process.argv[4]);
  const client = new AvipBrowserClient({
    url,
    requestId: "req-browser-001",
    deviceId: "browser-demo-001",
    capabilities: {
      audio_input: true,
      audio_output: true,
      camera: false,
      display: false,
    },
  });
  await client.connect({Origin: "http://127.0.0.1:8000"});
  const ready = await client.start();
  const uplink = client.sendUplink(payload);
  const downlink = await client.nextFrame();
  client.close();
  process.stdout.write(JSON.stringify({
    session_id: ready.session_id,
    request_id: ready.request_id,
    capabilities: ready.capabilities,
    uplink_hex: bytesToHex(uplink),
    downlink_hex: bytesToHex(downlink.raw),
    direction: downlink.direction,
    sequence: downlink.sequence,
    timestamp_us: downlink.timestampUs.toString(),
    payload_hex: bytesToHex(downlink.payload),
  }));
} else {
  process.stderr.write("usage: node cli.mjs encode | roundtrip\n");
  process.exit(2);
}
