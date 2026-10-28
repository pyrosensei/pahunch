const TARGET_RATE = 16000;
const CHUNK_SAMPLES = TARGET_RATE / 10; // 100 ms per WS frame

class MicResampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this._ratio = sampleRate / TARGET_RATE;
    this._tail = new Float32Array(0);
    this._outTail = new Float32Array(0);
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) {
      return true;
    }

    const merged = new Float32Array(this._tail.length + channel.length);
    merged.set(this._tail, 0);
    merged.set(channel, this._tail.length);

    const available = Math.floor(merged.length / this._ratio);
    const out = new Float32Array(available);
    for (let i = 0; i < available; i++) {
      const pos = i * this._ratio;
      const idx = Math.floor(pos);
      const frac = pos - idx;
      const next = Math.min(idx + 1, merged.length - 1);
      out[i] = merged[idx] + (merged[next] - merged[idx]) * frac;
    }
    const consumed = Math.floor(available * this._ratio);
    this._tail = merged.slice(consumed);

    let energy = 0;
    for (let i = 0; i < out.length; i++) {
      energy += out[i] * out[i];
    }
    const rms = Math.sqrt(energy / Math.max(1, out.length));

    const joined = new Float32Array(this._outTail.length + out.length);
    joined.set(this._outTail, 0);
    joined.set(out, this._outTail.length);
    let offset = 0;
    while (joined.length - offset >= CHUNK_SAMPLES) {
      const chunk = joined.subarray(offset, offset + CHUNK_SAMPLES);
      const pcm = new Int16Array(chunk.length);
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
      this.port.postMessage({ type: "audio", buffer: pcm.buffer }, [pcm.buffer]);
      offset += CHUNK_SAMPLES;
    }
    this._outTail = joined.slice(offset);

    this.port.postMessage({ type: "level", rms });
    return true;
  }
}

registerProcessor("mic-resampler", MicResampler);
