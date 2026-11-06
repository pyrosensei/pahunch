// Queue underruns are pauses in synthesis, not completion of agent playback.
class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunks = [];
    this._queuedSamples = 0;
    this._started = false;
    this._underruns = 0;
    this._fadeIn = 0;
    this._offset = 0;
    this._generation = null;
    this._retired = 0;
    this._ended = false;
    this._connection = 0;
    this._rate = sampleRate;
    this._bufferSamples = Math.round(sampleRate * 0.12);
    this.port.onmessage = (event) => {
      const message = event.data || {};
      if (message.type === "connection" && message.connection_id > this._connection) {
        this._connection = message.connection_id;
        this._chunks = [];
        this._queuedSamples = 0;
        this._started = false;
        this._underruns = 0;
        this._fadeIn = 0;
        this._offset = 0;
        this._generation = null;
        this._retired = 0;
        this._ended = false;
        return;
      }
      if ((message.connection_id || 0) !== this._connection) return;
      if (message.type === "flush") {
        this._retired = Math.max(this._retired, message.generation_id || this._generation || 0);
        this._chunks = [];
        this._queuedSamples = 0;
        this._started = false;
        this._underruns = 0;
        this._fadeIn = 0;
        this._offset = 0;
        this._generation = null;
        this._ended = false;
        return;
      }
      if (message.type === "begin" && message.generation_id > this._retired &&
          (this._generation === null || message.generation_id > this._generation)) {
        this._chunks = [];
        this._queuedSamples = 0;
        this._started = false;
        this._underruns = 0;
        this._fadeIn = 0;
        this._offset = 0;
        this._generation = message.generation_id;
        this._ended = false;
      }
      if (message.generation_id !== this._generation) return;
      if (message.type === "pcm" && message.samples && !this._ended) {
        this._chunks.push(message.samples);
        this._queuedSamples += message.samples.length;
      }
      if (message.type === "end") this._ended = true;
    };
  }

  process(inputs, outputs) {
    const output = outputs[0] && outputs[0][0];
    if (!output) {
      return true;
    }
    output.fill(0);
    const target = this._underruns ? this._bufferSamples * 2 : this._bufferSamples;
    if (!this._started && !this._ended && this._queuedSamples < target) return true;
    this._started = this._queuedSamples > 0;
    let written = 0;
    while (written < output.length && this._chunks.length > 0) {
      const current = this._chunks[0];
      const available = current.length - this._offset;
      const take = Math.min(output.length - written, available);
      output.set(current.subarray(this._offset, this._offset + take), written);
      this._offset += take;
      written += take;
      this._queuedSamples -= take;
      if (this._offset >= current.length) {
        this._chunks.shift();
        this._offset = 0;
      }
    }
    const ramp = Math.max(1, Math.round(this._rate * 0.005));
    for (let i = 0; i < written && this._fadeIn > 0; i++) {
      output[i] *= 1 - this._fadeIn / ramp;
      this._fadeIn--;
    }
    if (this._started && this._queuedSamples === 0 && !this._ended) {
      const fade = Math.min(written, ramp);
      for (let i = 0; i < fade; i++) output[written - fade + i] *= (fade - i - 1) / fade;
      this._started = false;
      this._fadeIn = ramp;
      this._underruns++;
      this.port.postMessage({ type: "underrun", generation_id: this._generation,
        connection_id: this._connection, count: this._underruns });
    }
    if (this._ended && this._chunks.length === 0 && this._generation !== null) {
      this.port.postMessage({ type: "drained", generation_id: this._generation,
        connection_id: this._connection });
      this._retired = this._generation;
      this._generation = null;
      this._ended = false;
    }
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
