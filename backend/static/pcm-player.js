// Queue underruns are pauses in synthesis, not completion of agent playback.
class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunks = [];
    this._offset = 0;
    this._generation = null;
    this._retired = 0;
    this._ended = false;
    this._connection = 0;
    this.port.onmessage = (event) => {
      const message = event.data || {};
      if (message.type === "connection" && message.connection_id > this._connection) {
        this._connection = message.connection_id;
        this._chunks = [];
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
        this._offset = 0;
        this._generation = null;
        this._ended = false;
        return;
      }
      if (message.type === "begin" && message.generation_id > this._retired &&
          (this._generation === null || message.generation_id > this._generation)) {
        this._chunks = [];
        this._offset = 0;
        this._generation = message.generation_id;
        this._ended = false;
      }
      if (message.generation_id !== this._generation) return;
      if (message.type === "pcm" && message.samples && !this._ended) {
        this._chunks.push(message.samples);
      }
      if (message.type === "end") this._ended = true;
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
