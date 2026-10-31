const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const staticDir = path.join(__dirname, '../static');

function worklet() {
  let Player;
  const context = vm.createContext({
    AudioWorkletProcessor: class {
      constructor() { this.port = { sent: [], postMessage(e) { this.sent.push(e); } }; }
    },
    registerProcessor(name, constructor) { Player = constructor; },
  });
  vm.runInContext(fs.readFileSync(path.join(staticDir, 'pcm-player.js'), 'utf8'), context);
  const player = new Player();
  const send = (message) => player.port.onmessage({ data: message });
  const tick = () => {
    const samples = new Float32Array(128);
    player.process([], [[samples]]);
    return samples;
  };
  return { player, send, tick };
}

test('queue underrun cannot signal playback completion before generation end', () => {
  const { player, send, tick } = worklet();
  send({ type: 'begin', generation_id: 1 });
  send({ type: 'pcm', generation_id: 1, samples: new Float32Array([0.5]) });
  assert.equal(tick()[0], 0.5);
  tick();
  assert.equal(player.port.sent.length, 0);
  send({ type: 'pcm', generation_id: 1, samples: new Float32Array([0.25]) });
  send({ type: 'end', generation_id: 1 });
  assert.equal(tick()[0], 0.25);
  tick();
  assert.equal(player.port.sent.length, 1);
  assert.equal(player.port.sent[0].type, 'drained');
  assert.equal(player.port.sent[0].generation_id, 1);
});

test('flushed generations cannot play late chunks or end a newer generation', () => {
  const { player, send, tick } = worklet();
  send({ type: 'begin', generation_id: 1 });
  send({ type: 'pcm', generation_id: 1, samples: new Float32Array([0.5]) });
  send({ type: 'flush', generation_id: 1 });
  send({ type: 'begin', generation_id: 2 });
  send({ type: 'pcm', generation_id: 1, samples: new Float32Array([0.9]) });
  send({ type: 'end', generation_id: 1 });
  send({ type: 'pcm', generation_id: 2, samples: new Float32Array([0.25]) });
  send({ type: 'end', generation_id: 2 });
  assert.equal(tick()[0], 0.25);
  tick();
  assert.equal(player.port.sent.length, 1);
  assert.equal(player.port.sent[0].generation_id, 2);
});

test('empty generation completes once, only after its explicit end', () => {
  const { player, send, tick } = worklet();
  send({ type: 'begin', generation_id: 4 });
  tick();
  assert.equal(player.port.sent.length, 0);
  send({ type: 'end', generation_id: 4 });
  tick(); tick();
  assert.equal(player.port.sent.length, 1);
  assert.equal(player.port.sent[0].generation_id, 4);
});

test('new connection can restart generation numbering without replaying old audio', () => {
  const { player, send, tick } = worklet();
  send({ type: 'connection', connection_id: 1 });
  send({ type: 'begin', connection_id: 1, generation_id: 8 });
  send({ type: 'end', connection_id: 1, generation_id: 8 });
  tick();
  send({ type: 'connection', connection_id: 2 });
  send({ type: 'begin', connection_id: 2, generation_id: 1 });
  send({ type: 'pcm', connection_id: 1, generation_id: 8, samples: new Float32Array([0.9]) });
  send({ type: 'pcm', connection_id: 2, generation_id: 1, samples: new Float32Array([0.25]) });
  send({ type: 'end', connection_id: 2, generation_id: 1 });
  assert.equal(tick()[0], 0.25);
  assert.equal(player.port.sent.at(-1).connection_id, 2);
  assert.equal(player.port.sent.at(-1).generation_id, 1);
});

function browser() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      style: {}, classList: { add() {}, remove() {} }, children: [],
      appendChild(child) { this.children.push(child); }, addEventListener() {},
    });
    return elements.get(id);
  }
  const sockets = [];
  class WebSocket {
    constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    close() {
      this.readyState = 3;
      if (this.onclose) this.onclose();
      if (this.closeListener) this.closeListener();
    }
    addEventListener(kind, fn) { if (kind === 'close') this.closeListener = fn; }
  }
  const player = { connect() {}, port: { sent: [], postMessage(e) { this.sent.push(e); } } };
  const context = vm.createContext({
    document: { getElementById: element, createElement: () => element(Symbol()) },
    location: { protocol: 'http:', host: 'local.test' }, WebSocket,
    fetch: async () => ({ json: async () => [] }),
    setTimeout: () => 0, clearTimeout() {},
    atob: (value) => Buffer.from(value, 'base64').toString('binary'),
    AudioWorkletNode: class { constructor() { return player; } },
    AudioContext: class {
      constructor() { this.sampleRate = 16000; this.audioWorklet = { async addModule() {} }; }
      async resume() {}
    },
  });
  const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
  vm.runInContext(html.split('<script>')[1].split('</script>')[0] +
    '\nglobalThis.app = { state, connectSocket, ensureAudio, ensureMic, playAudio, startCall, sendTypedLine };', context);
  context.app.state.player = player;
  context.app.state.audioCtx = { sampleRate: 16000 };
  const event = async (socket, data) => {
    socket.onmessage({ data: JSON.stringify(data) });
    await Promise.resolve(); await Promise.resolve();
  };
  return { app: context.app, sockets, event, player, context, element };
}

test('obsolete socket callbacks cannot change a replacement connection', async () => {
  const { app, sockets, event } = browser();
  await app.connectSocket('first');
  const first = sockets[0];
  await app.connectSocket('second');
  const second = sockets[1];
  assert.equal(first.readyState, 3);
  await event(first, { type: 'listening' });
  assert.equal(app.state.streaming, false);
  assert.equal(app.state.ws, second);
  first.onclose();
  assert.equal(app.state.ws, second);
});

test('late cancelled output is ignored by the browser', async () => {
  const { app, sockets, event, player } = browser();
  await app.connectSocket('session');
  const socket = sockets[0];
  await event(socket, { type: 'agent_audio_chunk', generation_id: 1, chunk_base64: 'AQI=' });
  await event(socket, { type: 'stop_speaking', generation_id: 1 });
  const count = player.port.sent.length;
  await event(socket, { type: 'agent_audio_chunk', generation_id: 1, chunk_base64: 'AQI=' });
  await event(socket, { type: 'agent_audio_end', generation_id: 1 });
  assert.equal(player.port.sent.length, count);
  await event(socket, { type: 'agent_audio_chunk', generation_id: 3, chunk_base64: 'AQI=' });
  assert.equal(app.state.generationId, 3);
});

test('blob decode finishing after cancellation cannot start a source', async () => {
  const { app, sockets, event } = browser();
  let resolveDecode;
  let sources = 0;
  app.state.audioCtx.decodeAudioData = () => new Promise(resolve => { resolveDecode = resolve; });
  app.state.audioCtx.createBufferSource = () => { sources++; return {}; };
  await app.connectSocket('session');
  const socket = sockets[0];
  await event(socket, { type: 'agent_audio', generation_id: 1, audio_base64: 'AQI=' });
  await event(socket, { type: 'stop_speaking', generation_id: 1 });
  resolveDecode({});
  await Promise.resolve(); await Promise.resolve();
  assert.equal(sources, 0);
});

test('browser acknowledges only final drain from its current connection', async () => {
  const { app, sockets, event, player } = browser();
  app.state.audioCtx = null;
  await app.ensureAudio();
  await app.connectSocket('first');
  const socket = sockets[0];
  await event(socket, { type: 'agent_audio_chunk', generation_id: 1, chunk_base64: 'AQI=' });
  player.port.onmessage({ data: { type: 'drained', connection_id: 1, generation_id: 1 } });
  assert.equal(socket.sent.length, 0);
  await event(socket, { type: 'agent_audio_end', generation_id: 1 });
  assert.equal(socket.sent.length, 0);
  player.port.onmessage({ data: { type: 'drained', connection_id: 0, generation_id: 1 } });
  assert.equal(socket.sent.length, 0);
  player.port.onmessage({ data: { type: 'drained', connection_id: 1, generation_id: 1 } });
  assert.equal(socket.sent[0].type, 'playback_done');
  assert.equal(socket.sent[0].generation_id, 1);
  assert.equal(socket.sent[1].type, 'start');
});

test('microphone permission resolving after disconnect releases its tracks', async () => {
  const { app, sockets, context } = browser();
  let resolveMic;
  let stopped = 0;
  context.navigator = { mediaDevices: {
    getUserMedia: () => new Promise(resolve => { resolveMic = resolve; }),
  } };
  await app.connectSocket('session');
  const pending = app.ensureMic();
  await Promise.resolve();
  sockets[0].close();
  resolveMic({ getTracks: () => [{ stop() { stopped++; } }] });
  await pending;
  assert.equal(stopped, 1);
  assert.equal(app.state.node, null);
  assert.equal(app.state.micStream, null);
});

test('browser requests server audio feedback and displays calibration', async () => {
  const { app, sockets, event, element } = browser();
  await app.connectSocket('session');
  const socket = sockets[0];
  await event(socket, { type: 'ready', state: 'DISCOVERY' });
  assert.equal(socket.sent.find(e => e.type === 'config').audio_feedback, true);
  await event(socket, { type: 'audio_level', rms: 20, threshold: 120, noise_floor: 20,
                        calibrating: true, noise_limited: false, speech: false });
  assert.match(element('mic-status').textContent, /quiet briefly/i);
});

test('no-speech hint stays visible until speech is detected', async () => {
  const { app, sockets, event, element } = browser();
  await app.connectSocket('session');
  const socket = sockets[0];
  await event(socket, { type: 'no_speech', reason: 'NO_SPEECH_DETECTED', elapsed_ms: 5000 });
  const hint = element('mic-status').textContent;
  assert.match(hint, /closer/i);
  await event(socket, { type: 'audio_level', rms: 20, threshold: 120, noise_floor: 20,
                        calibrating: false, noise_limited: false, speech: false });
  assert.equal(element('mic-status').textContent, hint);
  await event(socket, { type: 'speech_start' });
  assert.match(element('mic-status').textContent, /heard/i);
});

test('capture failure hint is actionable and old socket feedback is ignored', async () => {
  const { app, sockets, event, element } = browser();
  await app.connectSocket('first');
  await event(sockets[0], { type: 'no_speech', reason: 'NO_AUDIO_FRAMES', elapsed_ms: 5000 });
  assert.match(element('mic-status').textContent, /start mic/i);
  await app.connectSocket('second');
  await event(sockets[1], { type: 'listening' });
  const current = element('mic-status').textContent;
  await event(sockets[0], { type: 'no_speech', reason: 'EXCESSIVE_NOISE', elapsed_ms: 5000 });
  assert.equal(element('mic-status').textContent, current);
});

test('turn log displays wording source and treats transcript markup as text', async () => {
  const { app, sockets, event, element } = browser();
  await app.connectSocket('session');
  await event(sockets[0], {
    type: 'turn', generation_id: 1, state: 'DISCOVERY', transcript: '<img src=x>',
    agent_message: 'Hello, how can I help?', detected_language: 'en-IN',
    turn: { turn_count: 1, customer_confirmed: false },
    planner: { model: 'test', action: 'RESPOND', response_language: 'en-IN',
      wording_source: 'model', fallback_reason: null }, timings: {},
  });
  const entries = element('log').children;
  assert.equal(entries[0].children[1].textContent.includes('<img src=x>'), true);
  assert.equal(entries[0].innerHTML, undefined);
  const detail = entries.at(-1).children[1].textContent;
  assert.match(detail, /en-IN/);
  assert.match(detail, /model/);
});

test('typed input is shown only after backend redaction', async () => {
  const { app, element } = browser();
  await app.connectSocket('session');
  element('text-input').value = 'My OTP is 123456';
  app.sendTypedLine();
  assert.equal(element('text-input').value, '');
  assert.equal(JSON.stringify(element('log').children).includes('123456'), false);
});

for (const [initial, expected] of [
  ['CALLING', ['connect', 'context-confirmation']],
  ['CONNECTED', ['context-confirmation']],
  ['DISCOVERY', []],
]) {
  test(`browser advances scheduled ${initial} session before opening voice`, async () => {
    const { app, context, sockets } = browser();
    const transitions = [];
    app.state.audioCtx.resume = async () => {};
    context.fetch = async (url) => {
      if (url.endsWith('/call')) return { status: 409 };
      if (url.endsWith('/active-call')) return {
        ok: true, json: async () => ({ id: 'scheduled', state: initial }),
      };
      transitions.push(url.split('/').at(-1));
      return { ok: true, json: async () => ({ id: 'scheduled', state:
        url.endsWith('/connect') ? 'CONNECTED' : 'DISCOVERY' }) };
    };
    await app.startCall(1042);
    assert.deepEqual(transitions, expected);
    assert.equal(sockets.length, 1);
  });
}

test('expired scheduled call cannot open a voice socket after failed answer', async () => {
  const { app, context, sockets } = browser();
  app.state.audioCtx.resume = async () => {};
  context.fetch = async (url) => {
    if (url.endsWith('/call')) return { status: 409 };
    if (url.endsWith('/active-call')) return {
      ok: true, json: async () => ({ id: 'expired', state: 'CALLING' }),
    };
    return { ok: false, json: async () => ({ detail: 'Call has expired' }) };
  };
  await app.startCall(1042);
  assert.equal(sockets.length, 0);
  assert.equal(app.state.connecting, false);
});
