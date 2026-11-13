import type { Page } from '@playwright/test';

export const catalog = [
  {id:'headphones',name:'Studio Wireless Headphones',category:'AUDIO',description:'Over-ear comfort for everyday listening.',price_inr:2999,art:'headphones',direct_handoff_required:false},
  {id:'backpack',name:'Everyday Canvas Backpack',category:'CARRY',description:'A useful everyday companion.',price_inr:1499,art:'backpack',direct_handoff_required:false},
  {id:'watch',name:'Pulse Smartwatch',category:'WEARABLES',description:'Daily movement and music.',price_inr:4999,art:'watch',direct_handoff_required:false},
  {id:'laptop',name:'Airbook 14 Laptop',category:'COMPUTING',description:'A lightweight notebook for work.',price_inr:34999,art:'laptop',direct_handoff_required:true},
];

export const order = {
  id: 1050,
  merchant_name: 'SwiftCart',
  delivery_address_summary: 'Demo Residence, SwiftCart Test Block',
  window_start: '2026-11-01T12:15:00Z',
  window_end: '2026-11-01T14:15:00Z',
  payment_mode: 'COD',
  requires_otp: false,
  declared_value_inr: 1800,
  safe_drop_allowed: true,
  neighbour_drop_allowed: true,
  security_drop_allowed: true,
  status: 'PENDING',
};

export const session = {
  id: 'browser-test-session', order_id: order.id, state: 'DISCOVERY',
  started_at: '2026-11-01T12:00:00Z', updated_at: '2026-11-01T12:00:00Z',
  customer_confirmed: false, outcome: null, current_proposal_id: null,
  turn_count: 0, transport: 'browser', trigger_reason: 'OUT_FOR_DELIVERY',
  dispatch_state: null, phone_consent: false, proposal: null, verdict: null,
  courier_card: null,
};

export const planner = {
  model: 'mock-planner', action: 'RESPOND', confidence: 0.95, latency_ms: 20,
  prompt_tokens: 100, completion_tokens: 10, degraded: false, error_code: null,
  response_language: 'en-IN', wording_source: 'model', fallback_reason: null,
  review_needed: false, missing_fields: [], language_warning: null, awaiting_continuation: false,
};

export async function mockApi(page: Page, options: {
  failure?: boolean; sessions?: boolean; transcript?: string;
  transport?: 'browser' | 'twilio'; state?: string;
} = {}) {
  const requests: Array<{ method: string; path: string; body: unknown }> = [];
  let currentOrder = { ...order };
  let currentSession: typeof session & {retry_after?:string;remaining_attempts?:number} = { ...session, transport: options.transport || 'browser', state: options.state || session.state };
  let hasOrder = true;
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const body = request.postDataJSON();
    requests.push({ method, path, body });
    const fulfill = (json: unknown, status = 200) => route.fulfill({ status, json });
    if (options.failure) return fulfill({ detail: 'Demo backend unavailable' }, 503);
    if (path === '/api/operator/access') return fulfill({ authorized: true });
    if (path.endsWith('/courier-instructions')) return fulfill(null);
    if (path === '/api/catalog') return fulfill(catalog);
    if (path === '/api/orders' && method === 'POST') {
      const input = body as { payment_mode: string; product_id?: string; parcel_type?: string };
      const product = catalog.find(item=>item.id===input.product_id);
      currentOrder = { ...order, payment_mode: input.payment_mode,
        requires_otp: input.payment_mode === 'PREPAID',
        declared_value_inr: product?.price_inr || (input.parcel_type === 'high_value' ? 30000 : 1800) };
      hasOrder = true;
      return fulfill(currentOrder, 201);
    }
    if (path === '/api/orders') return fulfill(hasOrder ? [currentOrder] : []);
    if (path === `/api/orders/${order.id}/out-for-delivery`) {
      currentOrder = { ...currentOrder, status: 'OUT_FOR_DELIVERY' };
      return fulfill(currentOrder);
    }
    if (path === `/api/orders/${order.id}/call`) return fulfill(currentSession, 201);
    if (path === `/api/orders/${order.id}/active-call`) return fulfill(currentSession);
    if (path === `/api/orders/${order.id}`) return fulfill(currentOrder);
    if (path.endsWith('/slots')) return fulfill([]);
    if (path === '/api/sessions') {
      const active = new URL(request.url()).searchParams.get('active');
      const terminal = ['COMMITTED', 'NO_ANSWER', 'UNREACHABLE', 'ESCALATED', 'TOOL_FAILURE', 'TURN_BUDGET_EXHAUSTED'].includes(currentSession.state);
      return fulfill(options.sessions === false || (active === 'true' && terminal) || (active === 'false' && !terminal) ? [] : [currentSession]);
    }
    if (path === '/api/dialer/status') return fulfill({
      running: true, transport: 'browser', active_calls: 1,
      orders: [{ order_id: order.id, skip_reason: 'ACTIVE_CALL' }],
      phone: { enabled: false }, last_error: null,
    });
    if (path === '/api/dialer/events') return fulfill([]);
    if (path.endsWith('/turns')) return fulfill(options.transcript ? [{
      id: 1, speaker: 'AGENT', text: options.transcript,
      created_at: session.started_at, structured_payload: { planner },
    }] : []);
    if (path.endsWith('/events')) return fulfill([]);
    if (path.endsWith('/decline')) {
      currentSession = {...currentSession,state:'DECLINED',retry_after:new Date(Date.now()+30000).toISOString(),remaining_attempts:2};
      return fulfill(currentSession);
    }
    if (path.endsWith('/connect')) {
      currentSession = { ...currentSession, state: 'CONNECTED' };
      return fulfill(currentSession);
    }
    if (path.endsWith('/context-confirmation')) {
      currentSession = { ...currentSession, state: 'DISCOVERY' };
      return fulfill(currentSession);
    }
    if (path.endsWith('/commit')) {
      const courierCard = { order_id: order.id, action: 'HAND_TO_CUSTOMER',
        window_start: order.window_start, window_end: order.window_end,
        call_on_arrival: true, instructions: ['Hand directly to the customer.'],
        generated_at: session.started_at };
      return fulfill({ state: 'COMMITTED', commit_id: 'mock-commit',
        idempotency_key: (body as { idempotency_key: string }).idempotency_key,
        courier_card: courierCard, reused: false });
    }
    if (path === `/api/sessions/${session.id}`) return fulfill(currentSession);
    return fulfill({ detail: `Unexpected mocked API route: ${method} ${path}` }, 404);
  });
  return requests;
}

/** Prevent provider/network audio and permission prompts in browser acceptance tests. */
export async function mockAudio(page: Page) {
  await page.addInitScript(() => {
    class FakePort {
      onmessage: ((event: { data: unknown }) => void) | null = null;
      connection = 0;
      postMessage(message: { type: string; connection_id?: number; generation_id?: number }) {
        if (message.type === 'connection') this.connection = message.connection_id || 0;
        if (message.type === 'end') queueMicrotask(() => this.onmessage?.({ data: {
          type: 'drained', connection_id: this.connection, generation_id: message.generation_id,
        } }));
      }
      close() {}
    }
    class FakeAudioContext {
      sampleRate = 16000;
      state = 'running';
      destination = {};
      audioWorklet = { addModule: async () => {} };
      resume = async () => {};
      close = async () => { this.state = 'closed'; };
      suspend = async () => { this.state = 'suspended'; };
      createMediaStreamSource = () => ({ connect() {}, disconnect() {} });
    }
    class FakeAudioWorkletNode {
      port = new FakePort();
      connect() {}
      disconnect() {}
    }
    Object.defineProperty(window, 'AudioContext', { value: FakeAudioContext, configurable: true });
    Object.defineProperty(window, 'AudioWorkletNode', { value: FakeAudioWorkletNode, configurable: true });
  });
}
