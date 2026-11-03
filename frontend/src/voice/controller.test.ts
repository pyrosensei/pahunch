import { describe, expect, it, vi } from 'vitest';
import { VoiceController } from './controller';
import type { JsonObject, VoiceSession } from './controller';

class Socket {
  readyState = 1; binaryType = ''; sent: JsonObject[] = []; binary: ArrayBuffer[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null; onerror: (() => void) | null = null;
  send(value: string | ArrayBuffer) { if (typeof value === 'string') this.sent.push(JSON.parse(value)); else this.binary.push(value); }
  close() { this.readyState = 3; this.onclose?.(); }
  event(value: JsonObject) { this.onmessage?.({ data: JSON.stringify(value) }); }
}
class Worklet {
  messages: JsonObject[] = [];
  port = { onmessage: null as ((event: { data: JsonObject }) => void) | null,
    postMessage: (value: JsonObject) => this.messages.push(value) };
  connect = vi.fn(); disconnect = vi.fn();
  drain(generation: number, connection = 1) { this.port.onmessage?.({ data: { type: 'drained', generation_id: generation, connection_id: connection } }); }
}
const settle = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); };
function fixture(options: { transport?: string; state?: string; microphone?: () => Promise<MediaStream> } = {}) {
  const session: VoiceSession = { id: 'call-1', order_id: 42, transport: options.transport || 'browser', state: options.state || 'DISCOVERY', customer_confirmed: false };
  const requests: { url: string; method: string; body?: string }[] = [];
  const sockets: Socket[] = [];
  const player = new Worklet(), mic = new Worklet();
  const stopTrack = vi.fn();
  const stream = { getTracks: () => [{ stop: stopTrack }] } as unknown as MediaStream;
  const microphone = vi.fn(options.microphone || (() => Promise.resolve(stream)));
  const source = { connect: vi.fn(), disconnect: vi.fn() };
  const context = { sampleRate: 48000, state: 'running', destination: {},
    audioWorklet: { addModule: vi.fn().mockResolvedValue(undefined) }, resume: vi.fn().mockResolvedValue(undefined), close: vi.fn().mockResolvedValue(undefined),
    createMediaStreamSource: vi.fn(() => source),
  };
  const audio = vi.fn(() => context as unknown as AudioContext);
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push({ url, method: init?.method || 'GET', body: init?.body as string | undefined });
    const result = url.startsWith('/api/sessions?') ? [session] : url.endsWith('/commit') ? { state: 'COMMITTED', courier_card: { action: 'HAND_TO_CUSTOMER' } } : session;
    return new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
  const controller = new VoiceController({ fetch: fetcher, origin: 'https://demo.test',
    audio, microphone, worklet: (_context, name) => (name === 'pcm-player' ? player : mic) as unknown as AudioWorkletNode,
    socket: () => { const socket = new Socket(); sockets.push(socket); return socket as unknown as WebSocket; },
  });
  const connect = async () => { await controller.connect({ orderId: 42 }); sockets[0]?.event({ type: 'ready', state: session.state }); };
  return { controller, connect, requests, sockets, player, mic, microphone, audio, context, source, stopTrack, fetcher };
}

describe('voice controller connection and safety', () => {
  it('recovers recognition without closing the microphone or replaying a turn', async () => {
    const f = fixture(); await f.connect(); await f.controller.toggleMicrophone();
    f.sockets[0].event({ type: 'speech_end' });
    f.sockets[0].event({ type: 'no_speech', reason: 'RECOGNITION_EMPTY', recoverable: true });
    expect(f.controller.getSnapshot().hint).toContain('listening again');
    f.sockets[0].event({ type: 'listening' });
    expect(f.controller.getSnapshot().listening).toBe(true);
    expect(f.controller.getSnapshot().microphone).toBe(true);
    expect(f.controller.getSnapshot().error).toBeNull();
    expect(f.controller.getSnapshot().lines).toEqual([]);
    expect(f.sockets[0].sent.filter(event => event.type === 'scripted_turn')).toEqual([]);
    f.controller.disconnect();
  });
  it('preserves the initial idle prompt through a mount-cleanup cycle', () => {
    const f = fixture(); f.controller.disconnect();
    expect(f.controller.getSnapshot().hint).toBe('Choose an order, then connect when you are ready.');
  });
  it('resumes an active browser session without creating another call or requesting the microphone', async () => {
    const f = fixture(); await f.connect();
    expect(f.requests[0].url).toBe('/api/orders/42/active-call');
    expect(f.requests.some(request => request.method === 'POST')).toBe(false);
    expect(f.microphone).not.toHaveBeenCalled();
    expect(f.sockets[0].sent).toContainEqual({ type: 'config', tts_sample_rate: 48000, playback_tracking: true, audio_feedback: true });
    expect(f.sockets[0].sent.some(event => event.type === 'start')).toBe(false);
    f.controller.disconnect();
  });
  it.each([{ transport: 'phone', state: 'DISCOVERY' }, { transport: 'browser', state: 'COMMITTED' }])('refuses a read-only $transport/$state session before opening audio or a socket', async options => {
    const f = fixture(options); await f.controller.connect({ sessionId: 'call-1' });
    expect(f.sockets).toHaveLength(0); expect(f.audio).not.toHaveBeenCalled();
    expect(f.controller.getSnapshot().error).toBeTruthy();
  });
  it('shows only the redacted server transcript, never the raw typed value', async () => {
    const f = fixture(); await f.connect();
    expect(f.controller.sendText('My OTP is 123456')).toBe(true);
    expect(f.controller.getSnapshot().lines).toEqual([]);
    f.sockets[0].event({ type: 'turn', generation_id: 1, state: 'DISCOVERY', transcript: 'My OTP is [redacted]', agent_message: 'Please do not share your OTP.', turn: { customer_confirmed: false } });
    expect(f.controller.getSnapshot().lines.map(line => line.text).join(' ')).not.toContain('123456');
    expect(f.controller.getSnapshot().lines[0].text).toContain('[redacted]'); f.controller.disconnect();
  });
  it('resumes the scheduler-created session when creation races with admission', async () => {
    const f = fixture(); const normal = f.fetcher.getMockImplementation()!;
    let activeChecks = 0;
    f.fetcher.mockImplementation(async (input, options) => {
      if (String(input).endsWith('/active-call') && ++activeChecks === 1) return new Response('{}', { status: 404 });
      if (String(input).endsWith('/call') && options?.method === 'POST') return new Response('{}', { status: 409 });
      return normal(input, options);
    });
    await f.connect(); expect(activeChecks).toBe(2);
    expect(f.controller.getSnapshot().connected).toBe(true); f.controller.disconnect();
  });
  it.each(['Demo call capacity is occupied', 'Call cooldown is active', 'Order call history is resolved; human review required'])('preserves the actual admission refusal: %s', async detail => {
    const f = fixture();
    f.fetcher.mockImplementation(async input => new Response(JSON.stringify(String(input).endsWith('/call') ? { detail } : { detail: 'No active call found' }), { status: String(input).endsWith('/call') ? 409 : 404 }));
    await f.controller.connect({ orderId: 42 });
    expect(f.controller.getSnapshot().error).toBe(detail);
    expect(f.controller.getSnapshot().connecting).toBe(false);
    expect(f.sockets).toHaveLength(0); expect(f.audio).not.toHaveBeenCalled();
  });
  it('clears the previous conversation when a different route selection is chosen', async () => {
    const f = fixture(); await f.connect();
    f.sockets[0].event({ type: 'turn', generation_id: 1, state: 'DISCOVERY', transcript: 'previous customer', turn: {} });
    f.controller.resetSelection();
    expect(f.controller.getSnapshot().session).toBeNull(); expect(f.controller.getSnapshot().lines).toEqual([]);
    expect(f.sockets[0].readyState).toBe(3);
  });
  it('only commits after backend confirmation and uses a stable per-session idempotency key', async () => {
    const f = fixture(); await f.connect(); await f.controller.commit();
    expect(f.requests.some(request => request.url.endsWith('/commit'))).toBe(false);
    f.sockets[0].event({ type: 'turn', generation_id: 1, state: 'PENDING_CONFIRMATION', turn: { customer_confirmed: true } });
    await f.controller.commit();
    expect(JSON.parse(f.requests.find(request => request.url.endsWith('/commit'))!.body!)).toEqual({ idempotency_key: 'browser-call-1' });
    expect(f.controller.getSnapshot().session?.state).toBe('COMMITTED'); f.controller.disconnect();
  });
  it('revokes commit eligibility when a later turn changes the instructions', async () => {
    const f = fixture(); await f.connect();
    f.sockets[0].event({ type: 'turn', generation_id: 1, state: 'PENDING_CONFIRMATION', turn: { customer_confirmed: true } });
    f.sockets[0].event({ type: 'turn', generation_id: 2, state: 'NEGOTIATION', turn: { customer_confirmed: false } });
    await f.controller.commit();
    expect(f.requests.some(request => request.url.endsWith('/commit'))).toBe(false); f.controller.disconnect();
  });
  it('PC mode auto-saves only a backend-confirmed turn, never a proposal or revision notice', async () => {
    const f = fixture(); await f.connect(); f.controller.setAutoCommit(true);
    f.sockets[0].event({ type: 'turn', generation_id: 1, state: 'PENDING_CONFIRMATION', turn: { customer_confirmed: false } });
    f.sockets[0].event({ type: 'turn', generation_id: 2, state: 'PENDING_CONFIRMATION', planner: { operator_revision_id: 'new-plan' }, turn: { customer_confirmed: false } });
    expect(f.requests.some(request => request.url.endsWith('/commit'))).toBe(false);
    f.sockets[0].event({ type: 'turn', generation_id: 3, state: 'PENDING_CONFIRMATION', turn: { customer_confirmed: true } });
    await vi.waitFor(() => expect(f.controller.getSnapshot().session?.state).toBe('COMMITTED'));
    expect(f.requests.filter(request => request.url.endsWith('/commit'))).toHaveLength(1);
    f.controller.disconnect();
  });
  it('normal mode remains manual and a refused PC auto-save never claims success or retries', async () => {
    const f = fixture(); await f.connect();
    f.sockets[0].event({ type: 'turn', generation_id: 1, state: 'PENDING_CONFIRMATION', turn: { customer_confirmed: true } });
    expect(f.requests.some(request => request.url.endsWith('/commit'))).toBe(false);
    const normal = f.fetcher.getMockImplementation()!;
    const commitAttempts = vi.fn();
    f.fetcher.mockImplementation(async (input, options) => {
      if (String(input).endsWith('/commit')) { commitAttempts(); return new Response(JSON.stringify({ detail: 'The proposal changed. Confirm the revised plan first.' }), { status: 409 }); }
      return normal(input, options);
    });
    f.controller.setAutoCommit(true);
    f.sockets[0].event({ type: 'turn', generation_id: 2, state: 'PENDING_CONFIRMATION', turn: { customer_confirmed: true } });
    await vi.waitFor(() => expect(f.controller.getSnapshot().error).toContain('proposal changed'));
    expect(commitAttempts).toHaveBeenCalledOnce();
    expect(f.controller.getSnapshot().session?.courier_card).toBeFalsy(); f.controller.disconnect();
  });
});

describe('playback generations and cleanup', () => {
  it('shows the agent-initiated opening without inventing customer consent', async () => {
    const f=fixture(); await f.connect();
    f.sockets[0].event({type:'opening',generation_id:1,agent_message:'Your SwiftCart delivery is scheduled for this evening. Will you be available?'});
    expect(f.controller.getSnapshot().lines.some(line=>line.speaker==='agent' && line.text.includes('Will you be available?'))).toBe(true);
    expect(f.controller.getSnapshot().session?.customer_confirmed).toBe(false);
    f.controller.disconnect();
  });
  it('keeps microphone frames flowing during agent output for server barge-in detection', async () => {
    const f = fixture(); await f.connect(); await f.controller.toggleMicrophone();
    const ws = f.sockets[0];
    ws.event({ type: 'speech_end', duration_ms: 900 });
    ws.event({ type: 'agent_audio_chunk', generation_id: 1, chunk_base64: 'AAA=' });
    const buffer = new ArrayBuffer(640);
    f.mic.port.onmessage?.({ data: { type: 'audio', buffer } });
    expect(ws.binary).toEqual([buffer]);
    expect(f.controller.getSnapshot().listening).toBe(false);
    f.controller.sendText('I need to change my plan');
    f.mic.port.onmessage?.({ data: { type: 'audio', buffer } });
    expect(ws.binary).toHaveLength(1); f.controller.disconnect();
  });
  it('acknowledges drain only after generation end, once, and ignores cancelled audio', async () => {
    const f = fixture(); await f.connect(); const ws = f.sockets[0];
    ws.event({ type: 'agent_audio_chunk', generation_id: 1, chunk_base64: 'AAA=' });
    f.player.drain(1); expect(ws.sent.some(event => event.type === 'playback_done')).toBe(false);
    ws.event({ type: 'stop_speaking', generation_id: 1 });
    const before = f.player.messages.length;
    ws.event({ type: 'agent_audio_chunk', generation_id: 1, chunk_base64: 'AAA=' });
    expect(f.player.messages).toHaveLength(before);
    ws.event({ type: 'agent_audio_chunk', generation_id: 2, chunk_base64: 'AAA=' });
    ws.event({ type: 'agent_audio_end', generation_id: 2 });
    f.player.drain(2, 99); expect(ws.sent.some(event => event.type === 'playback_done')).toBe(false);
    f.player.drain(2); f.player.drain(2);
    expect(ws.sent.filter(event => event.type === 'playback_done')).toEqual([{ type: 'playback_done', generation_id: 2 }]);
    f.controller.disconnect();
  });
  it('ignores callbacks from a replaced socket and permits a new generation sequence', async () => {
    const f = fixture(); await f.connect(); const old = f.sockets[0];
    f.controller.disconnect(); await f.controller.connect({ orderId: 42 });
    const current = f.sockets[1]; current.event({ type: 'ready', state: 'DISCOVERY' });
    old.event({ type: 'turn', generation_id: 99, transcript: 'obsolete', state: 'COMMITTED' }); old.onclose?.();
    expect(f.controller.getSnapshot().connected).toBe(true);
    expect(f.controller.getSnapshot().lines).toHaveLength(0);
    current.event({ type: 'agent_audio_end', generation_id: 1 }); f.player.drain(1, 2);
    expect(current.sent).toContainEqual({ type: 'playback_done', generation_id: 1 }); f.controller.disconnect();
  });
  it('releases microphone permission that resolves after disconnect', async () => {
    let resolve!: (stream: MediaStream) => void;
    const permission = new Promise<MediaStream>(done => { resolve = done; });
    const stop = vi.fn(); const f = fixture({ microphone: () => permission }); await f.connect();
    const pending = f.controller.toggleMicrophone(); await settle(); f.controller.disconnect();
    resolve({ getTracks: () => [{ stop }] } as unknown as MediaStream); await pending;
    expect(stop).toHaveBeenCalledOnce(); expect(f.source.connect).not.toHaveBeenCalled();
    expect(f.controller.getSnapshot().microphone).toBe(false); expect(f.context.close).toHaveBeenCalled();
  });
  it('does not start capture after push-to-talk was released during permission prompt', async () => {
    let resolve!: (stream: MediaStream) => void;
    const f = fixture({ microphone: () => new Promise(done => { resolve = done; }) }); await f.connect();
    const pending = f.controller.beginPushToTalk(); await settle(); f.controller.endPushToTalk();
    resolve({ getTracks: () => [] } as unknown as MediaStream); await pending;
    expect(f.sockets[0].sent.some(event => event.type === 'start')).toBe(false); f.controller.disconnect();
  });
  it('gives an actionable permission error while keeping typed input available', async () => {
    const f = fixture({ microphone: () => Promise.reject(new Error('NotAllowedError')) }); await f.connect();
    await f.controller.toggleMicrophone();
    expect(f.controller.getSnapshot().error).toContain('type your reply');
    expect(f.controller.sendText('Please hand it to me')).toBe(true); f.controller.disconnect();
  });
});
