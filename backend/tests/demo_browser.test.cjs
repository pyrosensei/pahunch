const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page(name, fetch) {
  const elements = new Map();
  const element = () => ({ children: [], value: '', checked: false, disabled: false,
    appendChild(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    addEventListener() {}, setAttribute() {},
  });
  const document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }, createElement: element };
  const context = vm.createContext({ document, fetch, URLSearchParams,
    location: { search: '' }, setInterval() {}, Date, window: {} });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../static', name + '.js'), 'utf8'), context);
  return { ui: context.window.demo, get: document.getElementById, context };
}

test('shop sends fixed demo choices and requires an explicit readiness action', async () => {
  const requests = [];
  const { ui, get } = page('shop', async (url, options) => {
    requests.push([url, options]);
    return { ok: true, json: async () => ({ id: 1050, status: url.endsWith('out-for-delivery') ? 'OUT_FOR_DELIVERY' : 'PENDING', payment_mode: 'COD',
      window_start: '2026-10-31T12:15:00Z', window_end: '2026-10-31T14:15:00Z' }) };
  });
  get('payment').value = 'COD';
  get('parcel').value = 'standard';
  get('otp').checked = true;
  await ui.checkout({ preventDefault() {} });
  assert.equal(requests.length, 1);
  assert.deepEqual(JSON.parse(requests[0][1].body), { payment_mode: 'COD', parcel_type: 'standard', requires_otp: true });
  assert.equal(get('ready').disabled, false);
  await ui.markReady();
  assert.equal(requests[1][0], '/api/orders/1050/out-for-delivery');
  assert.equal(get('ready').disabled, true);
});

test('shop surfaces API failure without claiming an order was placed', async () => {
  const { ui, get } = page('shop', async () => ({ ok: false, json: async () => ({ detail: 'Checkout unavailable' }) }));
  await ui.checkout({ preventDefault() {} });
  assert.match(get('message').textContent, /Checkout unavailable/);
  assert.equal(get('ready').disabled, true);
});

test('dashboard renders transcript markup as text and shows policy and wording metadata', async () => {
  const { ui, get } = page('dashboard', async (url) => {
    const bodies = {
      '/api/orders': [], '/api/sessions?limit=100': [{ id: 's1', order_id: 1050, state: 'PENDING_CONFIRMATION', transport: 'browser', customer_confirmed: false,
        verdict: { valid: true, allowed_alternatives: [] }, proposal: { primary: { action: 'HAND_TO_CUSTOMER' } }, courier_card: null }],
      '/api/dialer/status': { running: true, transport: 'browser', orders: [{ order_id: 1050, skip_reason: 'ACTIVE_CALL' }] },
      '/api/sessions/s1/turns': [{ id: 1, speaker: 'AGENT', text: '<img src=x>', structured_payload: { planner: { action: 'PROPOSE_PLAN', response_language: 'en-IN', wording_source: 'model', fallback_reason: null } } }],
      '/api/sessions/s1/events': [], '/api/dialer/events?order_id=1050&limit=50': [],
    };
    return { ok: true, json: async () => bodies[url] || [] };
  });
  await ui.ready;
  assert.match(JSON.stringify(get('transcript').children), /<img src=x>/);
  assert.equal(get('transcript').innerHTML, undefined);
  assert.match(get('details').textContent, /HAND_TO_CUSTOMER/);
  assert.match(JSON.stringify(get('transcript').children), /wording.*model/);
});

test('dashboard explains pending phone permission using human-readable labels', async () => {
  const { ui, get } = page('dashboard', async url => ({ ok: true, json: async () => url.endsWith('/status')
    ? { running: true, transport: 'twilio', orders: [], phone: { enabled: true, configured: true,
      console_verified: true, streaming_verified: true, gnani_approved: false, session_approval_present: false } }
    : [] }));
  await ui.ready;
  assert.match(get('message').textContent, /Gnani permission/);
  assert.match(get('message').textContent, /approval for a phone test/);
});

test('dashboard marks displayed data as possibly stale after a failed refresh', async () => {
  const { ui, get } = page('dashboard', async () => ({ ok: false, json: async () => ({ detail: 'Read failed' }) }));
  await ui.ready;
  assert.match(get('message').textContent, /Read failed/);
  assert.match(get('message').textContent, /stale/);
});
