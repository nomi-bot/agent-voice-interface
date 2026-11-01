// AudioWorklet capture for the browser voice page.
// Posts a copy of each mono render quantum to the main thread. AVIP framing
// stays on the main thread (clients/browser/voice.js) so there is one encoder.

class AvipCaptureProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) {
      this.port.postMessage(new Float32Array(channel));
    }
    return true;
  }
}

registerProcessor("avip-capture", AvipCaptureProcessor);
