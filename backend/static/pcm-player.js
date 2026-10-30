/**
 * Streams raw PCM16 from the agent into the speakers.
 *
 * The server sends the agent's voice as raw PCM16 chunks so playback starts
 * ~400 ms after the request instead of waiting for the whole sentence. The
 * worklet keeps a small queue of Float32 chunks and emits silence when it
 * runs dry, which keeps the audio clock running without gaps.
 */
class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunks = [];
    this._offset = 0;
    this._hasData = false;
    this.port.onmessage = (event) => {
      const message = event.data || {};
      if (message.type === "flush") {
        this._chunks = [];
        this._offset = 0;
        this._hasData = false;
        return;
      }
      if (message.type === "pcm" && message.samples) {
        this._chunks.push(message.samples);
      }
    };
  }

  process(inputs, outputs) {
    const output = outputs[0] && outputs[0][0];
    if (!output) {
      return true;
    }
    let written = 0;
    while (written < output.length && this._chunks.length > 0) {
      const current = this._chunks[0];
      const available = current.length - this._offset;
      const take = Math.min(output.length - written, available);
      output.set(current.subarray(this._offset, this._offset + take), written);
      this._offset += take;
      written += take;
      if (this._offset >= current.length) {
        this._chunks.shift();
        this._offset = 0;
      }
    }
    if (written < output.length) {
      output.fill(0, written);
    }
    if (written > 0) {
      this._hasData = true;
    } else if (this._hasData) {
      this._hasData = false;
      this.port.postMessage({ type: "drained" });
    }
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
