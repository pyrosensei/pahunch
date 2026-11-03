export type JsonObject = Record<string, unknown>;
export interface VoiceOrder {
  id: number; merchant_name: string; payment_mode: string; status: string;
  requires_otp: boolean; window_start: string; window_end: string;
}
export interface VoiceSession {
  id: string; order_id: number; state: string; transport?: string;
  customer_confirmed?: boolean; proposal?: JsonObject | null;
  verdict?: { valid: boolean; violations?: { code: string }[] } | null;
  courier_card?: JsonObject | null;
}
export interface VoiceLine { id: number; speaker: 'agent' | 'customer' | 'system'; text: string; }
export interface VoiceSnapshot {
  connected: boolean; connecting: boolean; session: VoiceSession | null;
  microphone: boolean; listening: boolean; speaking: boolean; level: number;
  autoListen: boolean; hint: string; error: string | null;
  lines: VoiceLine[]; timings: Record<string, number>; wording: string | null;
  commitBusy: boolean;
}
interface WireEvent extends JsonObject {
  type: string; generation_id?: number; state?: string; transcript?: string;
  agent_message?: string; chunk_base64?: string; audio_base64?: string;
  turn?: { customer_confirmed?: boolean; verdict?: VoiceSession['verdict']; proposal?: JsonObject };
  planner?: { response_language?: string; wording_source?: string; fallback_reason?: string };
  timings?: Record<string, number>; detail?: string; reason?: string;
}
export interface VoiceEnvironment {
  fetch: typeof fetch;
  socket: (url: string) => WebSocket;
  audio: () => AudioContext;
  worklet: (context: AudioContext, name: string) => AudioWorkletNode;
  microphone: () => Promise<MediaStream>;
  origin: string;
}
const conversational = new Set(['DISCOVERY', 'NEGOTIATION', 'PENDING_CONFIRMATION']);
const resumable = new Set(['CALLING', 'CONNECTED', ...conversational]);
const noSpeechHints: Record<string, string> = {
  RECOGNITION_EMPTY: 'That audio did not produce a transcript. I am listening again - please repeat your instruction.',
  RECOGNITION_UNAVAILABLE: 'Speech recognition was interrupted. I am listening again - please repeat your instruction.',
  NO_AUDIO_FRAMES: 'Start your microphone, or type your reply below.',
  NO_SPEECH_DETECTED: 'Move a little closer and try speaking again.',
  EXCESSIVE_NOISE: 'The room is quite noisy. Try a quieter spot.',
  AUDIO_STREAM_STOPPED: 'Microphone audio stopped. Reconnect and try again.',
  SPEECH_TOO_SHORT: 'Please repeat your full instruction.',
};
const message = (error: unknown) => error instanceof Error ? error.message : 'Something went wrong. Please try again.';

/** Owns sockets and audio outside React rendering. Each instance owns one connection. */
export class VoiceController {
  private snapshot: VoiceSnapshot = {
    connected: false, connecting: false, session: null, microphone: false,
    listening: false, speaking: false, level: 0, autoListen: true,
    hint: 'Choose an order, then connect when you are ready.', error: null,
    lines: [], timings: {}, wording: null, commitBusy: false,
  };
  private listeners = new Set<() => void>();
  private env: VoiceEnvironment;
  private ws: WebSocket | null = null;
  private context: AudioContext | null = null;
  private audioPending: Promise<void> | null = null;
  private player: AudioWorkletNode | null = null;
  private microphoneNode: AudioWorkletNode | null = null;
  private microphoneSource: MediaStreamAudioSourceNode | null = null;
  private microphoneStream: MediaStream | null = null;
  private playing: AudioBufferSourceNode | null = null;
  private connectionId = 0;
  private actionEpoch = 0;
  private micEpoch = 0;
  private generation: number | null = null;
  private lastGeneration = 0;
  private ended = false;
  private output: 'pcm' | 'blob' | null = null;
  private blobDone = false;
  private pttHeld = false;
  private micPending = false;
  private capturing = false;
  private micHintReason: string | null = null;
  private request: AbortController | null = null;
  private lineId = 0;
  private detailRevision = 0;
  private autoCommit = false;

  constructor(environment?: Partial<VoiceEnvironment>) {
    this.env = {
      fetch: (...args) => fetch(...args), socket: url => new WebSocket(url),
      audio: () => new AudioContext(), worklet: (context, name) => new AudioWorkletNode(context, name),
      microphone: () => navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }),
      origin: typeof location === 'undefined' ? 'http://localhost' : location.origin,
      ...environment,
    };
  }
  getSnapshot = () => this.snapshot;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private update(patch: Partial<VoiceSnapshot>) { this.snapshot = { ...this.snapshot, ...patch }; this.listeners.forEach(listener => listener()); }
  private line(speaker: VoiceLine['speaker'], text: string) {
    this.update({ lines: [...this.snapshot.lines, { id: ++this.lineId, speaker, text }].slice(-150) });
  }
  private fail(error: unknown) { this.update({ error: message(error) }); }
  private async api<T>(path: string, options?: RequestInit): Promise<T> {
    const response = await this.env.fetch(path, { ...options, signal: this.request?.signal });
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'The request could not be completed.');
    return body as T;
  }
  private send(payload: JsonObject) { if (this.ws?.readyState === 1) this.ws.send(JSON.stringify(payload)); }

  async connect(selection: { sessionId?: string; orderId?: number }) {
    if (this.snapshot.connecting || this.snapshot.connected) return;
    const epoch = ++this.actionEpoch;
    this.request?.abort(); this.request = new AbortController();
    this.update({ connecting: true, error: null, hint: 'Preparing your conversation…' });
    try {
      let call: VoiceSession;
      if (selection.sessionId) {
        call = await this.api<VoiceSession>(`/api/sessions/${encodeURIComponent(selection.sessionId)}`);
      } else if (selection.orderId) {
        const active = await this.env.fetch(`/api/orders/${selection.orderId}/active-call`, { signal: this.request.signal });
        if (active.ok) call = await active.json();
        else if (active.status === 404) {
          const created = await this.env.fetch(`/api/orders/${selection.orderId}/call`, { method: 'POST', signal: this.request.signal });
          if (created.status === 409) {
            const body = await created.json();
            const creationError = new Error(typeof body.detail === 'string' ? body.detail : 'This delivery cannot start a conversation right now.');
            // A 409 may mean admission/cooldown, not a race with the scheduler.
            // Resume only a session that actually exists; never replace the cause with its lookup's 404.
            const resumed = await this.env.fetch(`/api/orders/${selection.orderId}/active-call`, { signal: this.request.signal });
            if (!resumed.ok) throw creationError;
            call = await resumed.json();
          }
          else {
            const body = await created.json();
            if (!created.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Could not start this conversation.');
            call = body;
          }
        } else throw new Error('Could not check the active conversation. Try again.');
      } else throw new Error('Choose an order before connecting.');
      if (epoch !== this.actionEpoch) return;
      const summaries = await this.api<VoiceSession[]>(`/api/sessions?order_id=${call.order_id}&limit=100`);
      const summary = summaries.find(item => item.id === call.id);
      if (!summary || summary.transport !== 'browser') throw new Error('This is not an available browser conversation. View it on the dashboard.');
      call = { ...call, ...summary };
      this.update({ session: call });
      if (!resumable.has(call.state)) throw new Error(`This conversation is ${call.state.toLowerCase().replaceAll('_', ' ')} and cannot be resumed.`);
      for (const [from, action] of [['CALLING', 'connect'], ['CONNECTED', 'context-confirmation']]) {
        if (call.state === from) call = { ...call, ...await this.api<VoiceSession>(`/api/sessions/${call.id}/${action}`, { method: 'POST' }) };
        if (epoch !== this.actionEpoch) return;
      }
      if (!conversational.has(call.state)) throw new Error('This conversation cannot accept voice right now.');
      try { await this.ensureAudio(); }
      catch { this.update({ hint: 'Audio output is unavailable. You can still type and read replies.' }); }
      if (epoch !== this.actionEpoch) return;
      this.update({ session: call, lines: [], timings: {}, wording: null });
      this.lastGeneration = 0;
      this.connectionId++;
      this.player?.port.postMessage({ type: 'connection', connection_id: this.connectionId });
      const base = this.env.origin.replace(/^http/, 'ws');
      const ws = this.env.socket(`${base}/ws/sessions/${encodeURIComponent(call.id)}/voice`);
      this.ws = ws; ws.binaryType = 'arraybuffer';
      ws.onmessage = event => {
        if (this.ws !== ws || typeof event.data !== 'string') return;
        try { void this.handle(JSON.parse(event.data) as WireEvent, ws).catch(error => { if (this.ws === ws) this.fail(error); }); }
        catch { this.fail(new Error('An unreadable voice event was received. Reconnect to try again.')); }
      };
      ws.onerror = () => { if (this.ws === ws) this.fail(new Error('The voice connection failed. Please reconnect.')); };
      ws.onclose = () => {
        if (this.ws !== ws) return;
        this.ws = null; this.stopPlayback(); this.releaseMic();
        this.update({ connected: false, connecting: false, listening: false, hint: 'Disconnected. Reconnect when you are ready.' });
      };
    } catch (error) { if (epoch === this.actionEpoch) { this.fail(error); this.update({ connecting: false }); } }
  }

  private async handle(event: WireEvent, ws: WebSocket) {
    if (this.ws !== ws) return;
    switch (event.type) {
      case 'ready':
        this.update({ connected: true, connecting: false, hint: 'Connected. Start your microphone or type below.', session: this.snapshot.session ? { ...this.snapshot.session, state: event.state || this.snapshot.session.state } : null });
        this.send({ type: 'config', tts_sample_rate: this.context ? Math.round(this.context.sampleRate) : 16000, playback_tracking: true, audio_feedback: true });
        if (this.snapshot.microphone && this.snapshot.autoListen) this.startListening();
        break;
      case 'listening': this.micHintReason = null; this.capturing = true; this.update({ listening: true, error: null, hint: 'Listening to you' }); break;
      case 'audio_level':
        if (event.speech || (this.micHintReason === 'NO_AUDIO_FRAMES' && Number(event.received_frames) > 0)) this.micHintReason = null;
        this.update({ hint: this.micHintReason ? noSpeechHints[this.micHintReason] : event.calibrating ? 'Adjusting to the room. Stay quiet briefly.' : event.noise_limited ? 'Try a quieter spot for clearer audio.' : event.speech ? 'Heard you' : 'Listening to you' });
        break;
      case 'no_speech': this.micHintReason = event.reason || 'NO_SPEECH_DETECTED'; this.update({ hint: noSpeechHints[this.micHintReason] || noSpeechHints.NO_SPEECH_DETECTED }); break;
      case 'speech_start': this.micHintReason = null; this.update({ hint: 'Heard you' }); break;
      case 'speech_end': this.update({ listening: false, hint: 'Preparing your reply…' }); break;
      case 'stop_speaking': if (event.generation_id === this.generation) { this.stopPlayback(); this.update({ hint: 'Go ahead, I’m listening.' }); } break;
      case 'opening': {
        if (!this.acceptGeneration(event.generation_id)) break;
        if (event.agent_message) this.line('agent', event.agent_message);
        this.update({ hint: 'The agent is explaining your delivery. Listen, then answer its question.' });
        break;
      }
      case 'turn': {
        if (!this.acceptGeneration(event.generation_id)) break;
        // Only the backend-redacted transcript is displayed, including for typed input.
        if (event.transcript) this.line('customer', event.transcript);
        if (event.agent_message) this.line('agent', event.agent_message);
        const current = this.snapshot.session;
        this.update({ session: current ? { ...current, state: event.state || current.state, customer_confirmed: !!event.turn?.customer_confirmed, verdict: event.turn?.verdict || current.verdict } : null,
          timings: event.timings || this.snapshot.timings,
          wording: event.planner ? `${event.planner.response_language || ''} · ${event.planner.wording_source || 'model'}${event.planner.fallback_reason ? ` · ${event.planner.fallback_reason}` : ''}` : null });
        if (event.state && !conversational.has(event.state)) this.releaseMic();
        void this.refreshPlan(ws);
        if (this.autoCommit && event.turn?.customer_confirmed) await this.commit();
        break;
      }
      case 'agent_audio_chunk':
        if (!this.acceptGeneration(event.generation_id)) break;
        this.update({ speaking: true, hint: 'Pahunch is speaking' });
        if (event.chunk_base64) this.playChunk(event.chunk_base64, event.generation_id!);
        break;
      case 'agent_audio':
        if (!this.acceptGeneration(event.generation_id)) break;
        if (event.audio_base64) { this.update({ speaking: true }); await this.playBlob(event.audio_base64, event.generation_id!, ws); }
        break;
      case 'agent_audio_end':
        if (!this.acceptGeneration(event.generation_id)) break;
        this.ended = true;
        this.update({ timings: event.timings || this.snapshot.timings });
        if (event.audio_error) this.update({ error: 'Audio could not be completed. Your reply is available as text.' });
        if (this.output === 'blob') { if (this.blobDone) this.finishPlayback(event.generation_id!, ws); }
        else if (this.player) {
          if (!this.output) this.player.port.postMessage({ type: 'begin', generation_id: event.generation_id, connection_id: this.connectionId });
          this.player.port.postMessage({ type: 'end', generation_id: event.generation_id, connection_id: this.connectionId });
        } else this.finishPlayback(event.generation_id!, ws);
        break;
      case 'error': this.fail(new Error(event.detail || 'The voice request could not be completed.')); break;
    }
  }

  private startListening() { if (this.ws?.readyState === 1) { this.capturing = true; this.send({ type: 'start' }); this.update({ listening: true }); } }
  setAutoListen(value: boolean) { this.update({ autoListen: value }); }
  setAutoCommit(value: boolean) { this.autoCommit = value; }
  sendText(text: string) {
    if (!text.trim() || this.ws?.readyState !== 1 || !this.snapshot.connected || !conversational.has(this.snapshot.session?.state || '')) return false;
    this.capturing = false;
    this.update({ autoListen: false, listening: false, error: null, hint: 'Preparing your reply…' });
    this.stopPlayback(); this.send({ type: 'reset' }); this.send({ type: 'scripted_turn', text: text.trim() });
    return true;
  }
  async toggleMicrophone() {
    if (this.snapshot.microphone) { if (this.snapshot.listening) this.send({ type: 'stop' }); this.releaseMic(); return; }
    if (!this.snapshot.connected) return;
    try { await this.ensureMic(); if (this.snapshot.microphone) this.startListening(); }
    catch { this.fail(new Error('Microphone access is unavailable. Check site permissions, or type your reply below.')); }
  }
  async beginPushToTalk() {
    if (!this.snapshot.connected) return;
    this.pttHeld = true; this.update({ autoListen: false, error: null });
    try {
      await this.ensureMic();
      if (!this.pttHeld || !this.snapshot.connected) return;
      this.stopPlayback(); this.send({ type: 'reset' }); this.startListening();
    } catch { this.pttHeld = false; this.fail(new Error('Microphone access is unavailable. You can type your reply instead.')); }
  }
  endPushToTalk() { if (!this.pttHeld) return; this.pttHeld = false; this.capturing = false; this.send({ type: 'stop' }); this.update({ listening: false, hint: 'Preparing your reply…' }); }

  private async refreshPlan(ws: WebSocket) {
    const session = this.snapshot.session;
    const revision = ++this.detailRevision;
    if (!session) return;
    try {
      const summaries = await this.api<VoiceSession[]>(`/api/sessions?order_id=${session.order_id}&limit=100`);
      const summary = summaries.find(item => item.id === session.id);
      if (this.ws !== ws || revision !== this.detailRevision || !summary || !this.snapshot.session) return;
      this.update({ session: { ...this.snapshot.session, proposal: summary.proposal, verdict: summary.verdict } });
    } catch { /* A failed detail refresh never replays a customer turn. */ }
  }

  private async ensureAudio() {
    if (this.audioPending) return this.audioPending;
    if (this.context && this.player) { await this.context.resume(); return; }
    const epoch = this.actionEpoch;
    const context = this.env.audio(); this.context = context;
    const pending = (async () => {
      try {
        await context.audioWorklet.addModule('/worklet.js');
        await context.audioWorklet.addModule('/pcm-player.js');
        if (epoch !== this.actionEpoch) { await context.close(); return; }
        const player = this.env.worklet(context, 'pcm-player'); this.player = player;
        player.port.onmessage = event => {
          if (event.data.type === 'drained' && event.data.connection_id === this.connectionId) this.finishPlayback(event.data.generation_id, this.ws);
        };
        player.connect(context.destination);
        await context.resume();
      } catch (error) { if (this.context === context) { this.context = null; this.player = null; } await context.close().catch(() => {}); throw error; }
    })();
    this.audioPending = pending;
    try { await pending; } finally { if (this.audioPending === pending) this.audioPending = null; }
  }
  private async ensureMic() {
    if (this.microphoneNode || this.micPending) return;
    this.micPending = true;
    const epoch = this.micEpoch;
    try {
      await this.ensureAudio();
      if (epoch !== this.micEpoch || !this.context || !this.snapshot.connected) return;
      const stream = await this.env.microphone();
      if (epoch !== this.micEpoch || this.microphoneNode || !this.snapshot.connected) { stream.getTracks().forEach(track => track.stop()); return; }
      const context = this.context;
      this.microphoneStream = stream;
      const source = context.createMediaStreamSource(stream); this.microphoneSource = source;
      const node = this.env.worklet(context, 'mic-resampler'); this.microphoneNode = node;
      node.port.onmessage = event => {
        if (epoch !== this.micEpoch) return;
        // Continue sending capture during agent output so the server can detect barge-in.
        if (event.data.type === 'audio' && this.capturing && this.ws?.readyState === 1) this.ws.send(event.data.buffer);
        if (event.data.type === 'level') this.update({ level: Math.min(1, Number(event.data.rms) * 4) });
      };
      source.connect(node); this.update({ microphone: true, hint: 'Microphone ready' });
    } catch (error) { this.releaseMic(); throw error; }
    finally { this.micPending = false; }
  }
  private releaseMic() {
    this.micEpoch++; this.pttHeld = false; this.capturing = false; this.micHintReason = null;
    this.microphoneStream?.getTracks().forEach(track => track.stop());
    this.microphoneSource?.disconnect();
    if (this.microphoneNode) { this.microphoneNode.port.onmessage = null; this.microphoneNode.disconnect(); }
    this.microphoneStream = null; this.microphoneSource = null; this.microphoneNode = null;
    this.update({ microphone: false, listening: false, level: 0 });
  }
  private stopPlayback() {
    const generation = this.generation;
    this.generation = null; this.ended = false; this.output = null; this.blobDone = false;
    if (this.playing) { this.playing.onended = null; try { this.playing.stop(); } catch { /* already stopped */ } this.playing = null; }
    this.player?.port.postMessage({ type: 'flush', generation_id: generation, connection_id: this.connectionId });
    this.update({ speaking: false });
  }
  private acceptGeneration(generation: number | undefined): generation is number {
    if (!Number.isInteger(generation) || generation! < this.lastGeneration) return false;
    if (generation === this.generation) return true;
    if (generation === this.lastGeneration) return false;
    this.stopPlayback(); this.lastGeneration = generation!; this.generation = generation!; return true;
  }
  private finishPlayback(generation: number, ws: WebSocket | null) {
    if (!ws || this.ws !== ws || this.generation !== generation || !this.ended) return;
    this.generation = null; this.update({ speaking: false, hint: 'Your turn' });
    if (ws.readyState !== 1) return;
    this.send({ type: 'playback_done', generation_id: generation });
    if (this.snapshot.microphone && this.snapshot.autoListen && conversational.has(this.snapshot.session?.state || '')) this.startListening();
  }
  private playChunk(base64: string, generation: number) {
    if (!this.player) return;
    if (this.output !== 'pcm') { this.output = 'pcm'; this.player.port.postMessage({ type: 'begin', generation_id: generation, connection_id: this.connectionId }); }
    const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
    const view = new DataView(bytes.buffer); const samples = new Float32Array(Math.floor(bytes.length / 2));
    for (let i = 0; i < samples.length; i++) { const value = view.getInt16(i * 2, true); samples[i] = value < 0 ? value / 32768 : value / 32767; }
    this.player.port.postMessage({ type: 'pcm', generation_id: generation, connection_id: this.connectionId, samples }, [samples.buffer]);
  }
  private async playBlob(base64: string, generation: number, ws: WebSocket) {
    this.output = 'blob';
    if (!this.context) { this.blobDone = true; return; }
    const context = this.context;
    try {
      const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
      const buffer = await context.decodeAudioData(bytes.buffer);
      if (this.ws !== ws || this.generation !== generation || this.context !== context) return;
      const source = context.createBufferSource(); this.playing = source;
      source.buffer = buffer; source.connect(context.destination);
      source.onended = () => {
        if (this.playing !== source || this.ws !== ws || this.generation !== generation) return;
        this.playing = null; this.blobDone = true; this.finishPlayback(generation, ws);
      };
      source.start();
    } catch { if (this.ws === ws && this.generation === generation) { this.blobDone = true; this.fail(new Error('Audio playback failed. Please read the reply below.')); this.finishPlayback(generation, ws); } }
  }

  async commit() {
    const session = this.snapshot.session;
    if (!session || !this.snapshot.connected || !session.customer_confirmed || this.snapshot.commitBusy) return;
    const epoch = this.actionEpoch;
    this.update({ commitBusy: true, error: null });
    try {
      const result = await this.api<{ state: string; courier_card: JsonObject }>(`/api/sessions/${session.id}/commit`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ idempotency_key: `browser-${session.id}` }) });
      if (epoch !== this.actionEpoch) return;
      this.releaseMic();
      this.update({ session: { ...session, state: result.state, courier_card: result.courier_card }, hint: 'Delivery plan saved for your courier.' });
    } catch (error) { if (epoch === this.actionEpoch) this.fail(error); }
    finally { if (epoch === this.actionEpoch) this.update({ commitBusy: false }); }
  }
  disconnect() {
    const wasActive = this.snapshot.connected || this.snapshot.connecting || this.snapshot.session !== null;
    this.actionEpoch++; this.request?.abort(); this.request = null;
    const ws = this.ws; this.ws = null; if (ws && ws.readyState < 2) ws.close();
    this.stopPlayback(); this.releaseMic();
    if (this.player) { this.player.port.onmessage = null; this.player.disconnect(); this.player = null; }
    const context = this.context; this.context = null;
    if (context && context.state !== 'closed') void context.close().catch(() => {});
    this.update({ connected: false, connecting: false, commitBusy: false, hint: wasActive ? 'Disconnected. Reconnect when you are ready.' : 'Choose an order, then connect when you are ready.' });
  }
  resetSelection() {
    this.disconnect(); this.detailRevision++;
    this.update({ session: null, error: null, lines: [], timings: {}, wording: null, hint: 'Choose an order, then connect when you are ready.' });
  }
}
