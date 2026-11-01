// Node exercises for AvipBrowserClient lifecycle. Python tests spawn this file.
// `clock` needs no server. The other commands take a ws:// URL.

import {
  AvipBrowserClient,
  MAX_PENDING_MESSAGES,
} from "./avip-client.js";

function clientFor(url) {
  return new AvipBrowserClient({
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
}

const command = process.argv[2];
const url = process.argv[3];

if (command === "clock") {
  const client = new AvipBrowserClient({
    url: "ws://127.0.0.1/ws/avip",
    requestId: "req-browser-001",
    deviceId: "browser-demo-001",
    capabilities: {audio_input: true, audio_output: true},
  });
  client.state = "open";
  client.started = true;
  const payload = new Uint8Array([0, 0]);
  client.socket = {
    readyState: 1,
    send() {
      throw new Error("send failed");
    },
    close() {},
  };
  let sendError = null;
  try {
    client.sendUplink(payload);
  } catch (error) {
    sendError = error.message;
  }
  const afterFail = {sequence: client.sequence, timestamp: client.timestampUs.toString()};
  client.socket = {
    readyState: 1,
    send() {},
    close() {},
  };
  client.sendUplink(payload);
  const afterOk = {sequence: client.sequence, timestamp: client.timestampUs.toString()};
  const first = client.nextFrame();
  const second = client.nextFrame();
  client.close();
  const settled = await Promise.allSettled([first, second]);
  let third = null;
  try {
    await client.nextFrame();
  } catch (error) {
    third = error.message;
  }
  process.stdout.write(JSON.stringify({
    sendError,
    afterFail,
    afterOk,
    state: client.state,
    settled: settled.map((result) => result.status),
    reasons: settled.map((result) => result.reason && result.reason.message),
    third,
  }));
} else if (command === "handshake") {
  const client = clientFor(url);
  await client.connect({Origin: "http://127.0.0.1:8000"});
  let message = null;
  try {
    await client.start();
  } catch (error) {
    message = error.message;
  }
  process.stdout.write(JSON.stringify({
    state: client.state,
    message,
    started: client.started,
  }));
  if (client.state !== "closed" || message === null || client.started) process.exit(1);
} else if (command === "next-frame") {
  const client = clientFor(url);
  await client.connect({Origin: "http://127.0.0.1:8000"});
  await client.start();
  const first = client.nextFrame();
  const second = client.nextFrame();
  client.socket.send("arm");
  const settled = await Promise.allSettled([first, second]);
  let third = null;
  try {
    await client.nextFrame();
  } catch (error) {
    third = error.message;
  }
  process.stdout.write(JSON.stringify({
    state: client.state,
    settled: settled.map((result) => result.status),
    reasons: settled.map((result) => result.reason && result.reason.message),
    third,
  }));
  if (settled.some((result) => result.status !== "rejected") || client.state !== "closed") {
    process.exit(1);
  }
} else if (command === "slow") {
  const client = clientFor(url);
  await client.connect({Origin: "http://127.0.0.1:8000"});
  await client.start();
  const deadline = Date.now() + 2000;
  while (client.state !== "closed" && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  const drained = [];
  while (client.pending > 0) {
    drained.push((await client.nextFrame()).sequence);
  }
  let after = null;
  try {
    await client.nextFrame();
  } catch (error) {
    after = error.message;
  }
  process.stdout.write(JSON.stringify({
    state: client.state,
    failure: client.failure && client.failure.message,
    pending: client.pending,
    drained,
    after,
    bound: MAX_PENDING_MESSAGES,
  }));
  if (client.state !== "closed" || client.failure?.message !== "queue_full") process.exit(1);
} else {
  process.stderr.write("usage: node lifecycle.mjs clock|handshake|next-frame|slow [url]\n");
  process.exit(2);
}
