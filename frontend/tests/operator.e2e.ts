import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mockApi, mockAudio, order, planner, session } from './fixtures';
import type { OperatorSummary, Proposal } from '../src/pages/operator-types';

const TOKEN = 'synthetic-operator-access';
const CUSTOMER_NEED = 'Please hand the parcel directly to me in the scheduled window.';
const NEIGHBOUR_NEED = 'Please leave it with Asha at Flat 4B, Demo Lane.';
const SYSTEM_NOTE = 'Operator reviewed the proposed delivery window.';

test('PC participant keeps typing optional and automatically saves only the confirmed plan', async ({ page }) => {
  const requests = await mockApi(page);
  await mockAudio(page);
  let generation = 0;
  await page.routeWebSocket('**/ws/sessions/**/voice', socket => {
    setTimeout(() => socket.send(JSON.stringify({type:'ready',state:'DISCOVERY'})), 20);
    socket.onMessage(raw => {
      if(typeof raw !== 'string') return;
      const event = JSON.parse(raw);
      if(event.type !== 'scripted_turn') return;
      const confirmed = event.text === 'Yes';
      generation++;
      socket.send(JSON.stringify({type:'turn',generation_id:generation,state:'PENDING_CONFIRMATION',transcript:event.text,
        agent_message:confirmed?'Thank you. Your confirmation was received.':'May I confirm direct handoff during your scheduled window?',
        turn:{customer_confirmed:confirmed,verdict:{valid:true,violations:[]}},planner}));
      socket.send(JSON.stringify({type:'agent_audio_end',generation_id:generation}));
    });
  });
  await page.goto(`/voice?session_id=${session.id}&order_id=${order.id}&pc_demo=1`);
  await expect(page.getByRole('textbox',{name:'Your reply'})).toBeHidden();
  await expect(page.getByRole('button',{name:'Save delivery plan'})).toHaveCount(0);
  await expect(page.getByRole('link',{name:'Open main monitor'})).toHaveAttribute('target','_blank');
  await page.getByRole('button',{name:'Connect conversation'}).click();
  await page.getByText('Keyboard fallback',{exact:true}).click();
  const input=page.getByRole('textbox',{name:'Your reply'});
  await input.fill('Please hand it to me');
  await page.getByRole('button',{name:'Send message'}).click();
  await expect(page.getByText('May I confirm direct handoff during your scheduled window?')).toBeVisible();
  expect(requests.filter(r=>r.path.endsWith('/commit'))).toHaveLength(0);
  await input.fill('Yes');
  await page.getByRole('button',{name:'Send message'}).click();
  await expect(page.getByText('Plan saved',{exact:true})).toBeVisible();
  expect(requests.filter(r=>r.path.endsWith('/commit'))).toHaveLength(1);
});
async function operatorBackend(page: Page, options: { confirmed?: boolean; policyFailure?: boolean; neighbour?: boolean; turnPlanner?: Partial<typeof planner> } = {}) {
  await mockApi(page);
  const customerNeed = options.neighbour ? NEIGHBOUR_NEED : CUSTOMER_NEED;
  let summary: OperatorSummary = {
    session_id: session.id, order_id: order.id, state: 'PENDING_CONFIRMATION', outcome: null,
    transport: 'browser', dispatch_state: null, phone_consent: false,
    customer_requests: [{ turn_id: 1, text: customerNeed, created_at: session.started_at }],
    proposal: { id: 'proposal-one', source: 'customer', customer_confirmed: !!options.confirmed,
      plan: { primary: options.neighbour ? {
        action: 'LEAVE_WITH_NEIGHBOR', window_start: order.window_start, window_end: order.window_end, call_on_arrival: true,
        named_recipient: 'Asha', recipient_address: 'Flat 4B, Demo Lane', delay_minutes: null, max_retries: 0,
        recipient_source: { turn_id: 1, quote: 'Please leave it with Asha' },
        recipient_address_source: { turn_id: 1, quote: 'at Flat 4B, Demo Lane' },
      } : { action: 'HAND_TO_CUSTOMER', window_start: order.window_start, window_end: order.window_end, call_on_arrival: true, named_recipient: null, delay_minutes: null, max_retries: 0 }, fallback: { action: 'RETRY_AFTER_DELAY', delay_minutes: 15, max_retries: 1, call_on_arrival: true } },
      policy: { valid: true, violations: [], allowed_alternatives: options.neighbour ? ['LEAVE_WITH_NEIGHBOR', 'HAND_TO_CUSTOMER', 'RESCHEDULE'] : ['HAND_TO_CUSTOMER', 'RESCHEDULE'] } },
    courier_card: null, operator_actions: { can_reject: true, can_revise: true, reason: null },
    operator_events: [], summary: { status: options.confirmed ? 'CONFIRMED' : 'PENDING_CONFIRMATION', text: options.confirmed ? 'The customer confirmed the current plan. It has not been committed.' : 'The current proposal has not been confirmed by the customer or committed.' },
    confirmation: { confirmed: !!options.confirmed, requires_fresh_confirmation: !options.confirmed, presentation_pending: false },
  };
  const writes: { path: string; body: Record<string, unknown>; token: string | undefined }[] = [];
  const allRequests: string[] = [];
  await page.route('**/api/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    allRequests.push(`${request.method()} ${path}`);
    const fulfill = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/api/operator/access') return fulfill(request.headers()['x-operator-token'] === TOKEN ? {authorized:true} : {detail:'A valid operator token is required'}, request.headers()['x-operator-token'] === TOKEN ? 200 : 401);
    if (path === '/api/sessions') return fulfill([{ ...session, state: summary.state, current_proposal_id: summary.proposal?.id || null, customer_confirmed: summary.confirmation.confirmed, proposal: summary.proposal?.plan || null, verdict: summary.proposal?.policy || null }]);
    if (path.endsWith('/turns')) {
      const after = Number(url.searchParams.get('after_id'));
      return fulfill([
        { id: 1, speaker: 'CUSTOMER', text: customerNeed, created_at: session.started_at, structured_payload: {} },
        { id: 2, speaker: 'AGENT', text: 'May I take this plan forward?', created_at: session.started_at, structured_payload: { planner: { ...planner, ...options.turnPlanner } } },
        { id: 3, speaker: 'SYSTEM', text: SYSTEM_NOTE, created_at: session.started_at, structured_payload: { operator: { action: 'REVIEW' } } },
      ].filter(turn => turn.id > after));
    }
    if (path.endsWith('/slots')) return fulfill([
      { id: 1, start_at: order.window_start, end_at: order.window_end },
      { id: 2, start_at: '2026-11-02T12:15:00Z', end_at: '2026-11-02T14:15:00Z' },
    ]);
    if (!path.endsWith('/operator-summary') && !path.includes('/operator/')) return route.fallback();
    const token = request.headers()['x-operator-token'];
    if (token !== TOKEN) return fulfill({ detail: 'A valid operator token is required' }, 401);
    if (path.endsWith('/operator-summary')) return fulfill(summary);
    const body = request.postDataJSON() as Record<string, unknown>;
    writes.push({ path, body, token });
    if (body.expected_proposal_id !== summary.proposal?.id) return fulfill({ detail: 'The proposal changed. Refresh before reviewing it again' }, 409);
    if (path.endsWith('/revise') && options.policyFailure) return fulfill({ detail: 'The revised proposal violates delivery policy' }, 409);
    const previous = summary.proposal!.id;
    const action = path.endsWith('/reject') ? 'REJECT' : 'REVISE';
    summary = { ...summary,
      state: action === 'REJECT' ? 'NEGOTIATION' : 'PENDING_CONFIRMATION',
      proposal: action === 'REJECT' ? null : { ...summary.proposal!, id: 'proposal-two', plan: body.proposal as Proposal, source: 'operator', customer_confirmed: false },
      confirmation: { confirmed: false, requires_fresh_confirmation: true, presentation_pending: action === 'REVISE' },
      operator_actions: { can_reject: action === 'REVISE', can_revise: action === 'REVISE', reason: action === 'REJECT' ? 'Only an uncommitted pending proposal can be changed.' : null },
      operator_events: [{ id: 1, action, reason: String(body.reason), proposal_id: action === 'REJECT' ? null : 'proposal-two', previous_proposal_id: previous, created_at: session.started_at }],
      summary: { status: action === 'REJECT' ? 'NO_PROPOSAL' : 'PENDING_CONFIRMATION', text: action === 'REJECT' ? 'No current delivery proposal is available. Nothing has been committed.' : 'The current proposal has not been confirmed by the customer or committed.' },
    };
    return fulfill(summary);
  });
  return { writes, allRequests };
}
async function unlock(page: Page, value = TOKEN) {
  await page.getByRole('button', { name: /Enter operator key|Change operator key/ }).click();
  await page.getByLabel('Operator key', { exact: true }).fill(value);
  await page.getByRole('button', { name: 'Use operator key' }).click();
  await page.getByRole('tab', {name:'Review',exact:true}).click();
}

test('operator monitor is separate from customer PC voice and uses tab-only authenticated access', async ({ page }, testInfo) => {
  const backend = await operatorBackend(page); const sockets: string[] = []; page.on('websocket', socket => { if (socket.url().includes('/ws/sessions/')) sockets.push(socket.url()); });
  await page.goto('/dashboard');
  await expect(page.getByRole('heading', { name: 'Demo orders' })).toBeVisible();
  await expect(page.locator('.operations-transcript')).toContainText(CUSTOMER_NEED);
  await expect(page.getByRole('link', {name:'Start customer voice demo'})).toHaveCount(0);
  await page.getByRole('tab', {name:'Review',exact:true}).click();
  await expect(page.getByRole('button', { name: 'Start microphone' })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: 'Your reply' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Reject proposal', exact: true })).toBeDisabled();
  await unlock(page, 'incorrect-synthetic-key');
  await expect(page.getByText(/A valid operator token is required/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Reject proposal', exact: true })).toBeDisabled();
  await unlock(page);
  await expect(page.getByRole('button', { name: 'Reject proposal', exact: true })).toBeEnabled();
  expect(await page.evaluate(() => sessionStorage.getItem('pahunch.operator-token'))).toBe(TOKEN);
  expect(await page.evaluate(() => localStorage.getItem('pahunch.operator-token'))).toBeNull();
  expect(page.url()).not.toContain(TOKEN); expect(sockets).toEqual([]); expect(backend.writes).toEqual([]);
  const widths = await page.evaluate(() => ({ content: document.documentElement.scrollWidth, viewport: innerWidth }));
  expect(widths.content).toBeLessThanOrEqual(widths.viewport + 1);
  await page.screenshot({ path: testInfo.outputPath('operator-console.png'), fullPage: true, animations: 'disabled' });
});

test('operator revision sends the current version and requires fresh customer consent', async ({ page }) => {
  const backend = await operatorBackend(page, { confirmed: true });
  await page.goto('/dashboard'); await unlock(page);
  await expect(page.getByText('Customer confirmed · not yet committed')).toBeVisible();
  await page.getByRole('button', { name: 'Revise proposal', exact: true }).click();
  await page.getByRole('combobox', { name: 'Primary plan action' }).selectOption('RESCHEDULE');
  await page.getByRole('textbox', { name: 'Operator reason' }).fill('Customer requested a review of the scheduled handoff.');
  await page.getByRole('button', { name: 'Submit revision for customer confirmation' }).click();
  await expect(page.getByText('Revision proposed. A fresh customer confirmation is required.')).toBeVisible();
  await expect(page.getByText('Proposed · not customer confirmed')).toBeVisible();
  await expect(page.getByText('The agent must present the revised plan to the customer before confirmation can count.')).toBeVisible();
  expect(backend.writes).toHaveLength(1);
  expect(backend.writes[0].body).toMatchObject({ expected_proposal_id: 'proposal-one', reason: 'Customer requested a review of the scheduled handoff.', proposal: { primary: { action: 'RESCHEDULE' } } });
  expect(backend.writes[0].token).toBe(TOKEN);
  expect(backend.writes[0].body).not.toHaveProperty('customer_confirmed');
  expect(backend.allRequests.some(request => request.includes('/commit'))).toBe(false);
  await expect(page.getByRole('button', { name: 'Save delivery plan' })).toHaveCount(0);
});

test('operator sees the neighbour address and preserves untouched sources during a window revision', async ({ page }) => {
  const backend = await operatorBackend(page, { neighbour: true, confirmed: true });
  await page.goto('/dashboard'); await unlock(page);
  const decision = page.locator('.operator-plan');
  await expect(decision).toContainText('Asha');
  await expect(decision).toContainText('Flat 4B, Demo Lane');
  await decision.getByText('Customer source', { exact: true }).click();
  await expect(decision).toContainText('Please leave it with Asha');
  await expect(decision).toContainText('at Flat 4B, Demo Lane');
  await decision.getByRole('button', { name: 'Revise proposal', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Primary plan recipient name' })).toHaveValue('Asha');
  await expect(page.getByRole('textbox', { name: 'Primary plan recipient address' })).toHaveValue('Flat 4B, Demo Lane');
  const windowSelect = page.getByRole('combobox', { name: 'Primary plan delivery window' });
  await expect(windowSelect).toHaveValue('0');
  await windowSelect.selectOption('1');
  await page.getByRole('textbox', { name: 'Operator reason' }).fill('Move the handoff to the next recorded availability.');
  await page.getByRole('button', { name: 'Submit revision for customer confirmation' }).click();
  await expect(page.getByText('Revision proposed. A fresh customer confirmation is required.')).toBeVisible();
  expect(backend.writes).toHaveLength(1);
  const primary = (backend.writes[0].body.proposal as Proposal).primary;
  expect(primary).toMatchObject({
    action: 'LEAVE_WITH_NEIGHBOR', named_recipient: 'Asha', recipient_address: 'Flat 4B, Demo Lane',
    window_start: '2026-11-02T12:15:00Z', window_end: '2026-11-02T14:15:00Z',
    recipient_source: { turn_id: 1, quote: 'Please leave it with Asha' },
    recipient_address_source: { turn_id: 1, quote: 'at Flat 4B, Demo Lane' },
  });
  await expect(decision).toContainText('Flat 4B, Demo Lane');
  expect(backend.allRequests.some(request => request.includes('/commit'))).toBe(false);
});

test('recipient changes require a fresh location and clear the previous attribution', async ({ page }) => {
  const backend = await operatorBackend(page, { neighbour: true });
  await page.goto('/dashboard'); await unlock(page);
  const decision = page.locator('.operator-plan');
  await decision.getByRole('button', { name: 'Revise proposal', exact: true }).click();
  await page.getByRole('textbox', { name: 'Primary plan recipient name' }).fill('Asha Devi');
  await expect(page.getByRole('textbox',{name:'Primary plan recipient address'})).toHaveValue('');
  await page.getByRole('textbox',{name:'Primary plan recipient address'}).fill('Flat 4B, Demo Lane');
  await page.getByRole('textbox', { name: 'Operator reason' }).fill('Clarify the recipient name.');
  await page.getByRole('button', { name: 'Submit revision for customer confirmation' }).click();
  await expect(page.getByText('Revision proposed. A fresh customer confirmation is required.')).toBeVisible();
  let primary = (backend.writes[0].body.proposal as Proposal).primary;
  expect(primary).toMatchObject({ named_recipient: 'Asha Devi', recipient_source: null, recipient_address: 'Flat 4B, Demo Lane', recipient_address_source: null });
  await decision.getByRole('button', { name: 'Revise proposal', exact: true }).click();
  await page.getByRole('textbox', { name: 'Primary plan recipient address' }).fill('Flat 5B, Demo Lane');
  await page.getByRole('textbox', { name: 'Operator reason' }).fill('Clarify the recipient address.');
  await page.getByRole('button', { name: 'Submit revision for customer confirmation' }).click();
  await expect.poll(() => backend.writes.length).toBe(2);
  primary = (backend.writes[1].body.proposal as Proposal).primary;
  expect(primary).toMatchObject({ named_recipient: 'Asha Devi', recipient_source: null, recipient_address: 'Flat 5B, Demo Lane', recipient_address_source: null });
});

test('transcript diagnostics keep review-needed non-terminal and language warnings secondary', async ({ page }) => {
  await operatorBackend(page, { turnPlanner: {
    action: 'ASK_CLARIFICATION', review_needed: true, missing_fields: ['recipient_address'],
    language_warning: 'LOW_CONFIDENCE', error_code: 'LLM_STRUCTURED_OUTPUT',
    wording_source: 'fallback', fallback_reason: 'structured_output',
  } });
  await page.goto('/dashboard');
  await expect(page.getByRole('tab', { name: 'Conversation', exact: true })).toHaveAttribute('aria-selected','true');
  await page.getByText('Response details', { exact: true }).click();
  const meta = page.locator('.turn-meta');
  await expect(meta.getByRole('status')).toHaveText('Review helpful · conversation can continue');
  await expect(meta).toContainText('Missing: Recipient address');
  await expect(meta.locator('.language-warning code')).toHaveText('LOW_CONFIDENCE');
  await expect(meta.locator('.turn-error code')).toHaveText('LLM_STRUCTURED_OUTPUT');
  await expect(page.getByRole('button', { name: 'Start microphone' })).toHaveCount(0);
});

test('customer plan shows the recipient address and source without operator controls', async ({ page }) => {
  const prepared = { ...session, state: 'PENDING_CONFIRMATION', verdict: { valid: true, violations: [] }, proposal: {
    primary: { action: 'LEAVE_WITH_NEIGHBOR', window_start: order.window_start, window_end: order.window_end, call_on_arrival: true,
      named_recipient: 'Asha', recipient_address: 'Flat 4B, Demo Lane',
      recipient_source: { turn_id: 1, quote: 'Please leave it with Asha' },
      recipient_address_source: { turn_id: 1, quote: 'at Flat 4B, Demo Lane' } },
    fallback: null,
  } };
  await mockApi(page); await mockAudio(page);
  await page.route('**/api/sessions?**', route => route.fulfill({ json: [prepared] }));
  await page.route(`**/api/sessions/${session.id}`, route => route.fulfill({ json: prepared }));
  await page.routeWebSocket('**/ws/sessions/**/voice', socket => {
    setTimeout(() => socket.send(JSON.stringify({ type: 'ready', state: 'PENDING_CONFIRMATION' })), 20);
  });
  await page.goto(`/voice?session_id=${session.id}&order_id=${order.id}`);
  await page.getByRole('button', { name: 'Connect conversation' }).click();
  const plan = page.getByRole('region', { name: 'Your delivery plan' });
  await expect(plan).toContainText('Asha');
  await expect(plan).toContainText('Flat 4B, Demo Lane');
  await plan.getByText('Customer source', { exact: true }).click();
  await expect(plan).toContainText('Please leave it with Asha');
  await expect(plan).toContainText('at Flat 4B, Demo Lane');
  await expect(plan.getByRole('button', { name: 'Save delivery plan' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Reject proposal', exact: true })).toHaveCount(0);
});

test('rejecting a proposal records the reason and clears the displayed plan', async ({ page }) => {
  const backend = await operatorBackend(page, { confirmed: true });
  await page.goto('/dashboard'); await unlock(page);
  await page.getByRole('button', { name: 'Reject proposal', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Reject current proposal' })).toBeDisabled();
  await page.getByRole('textbox', { name: 'Operator reason' }).fill('The requested recipient needs to be clarified.');
  await page.getByRole('button', { name: 'Reject current proposal' }).click();
  await expect(page.getByText('Proposal rejected. Customer confirmation has been cleared.')).toBeVisible();
  await expect(page.getByText('No delivery plan has been proposed.')).toBeVisible();
  expect(backend.writes[0].body).toEqual({ expected_proposal_id: 'proposal-one', reason: 'The requested recipient needs to be clarified.' });
  await expect(page.getByRole('heading', { name: 'Operator decisions' })).toBeVisible();
});

test('policy rejection leaves the operator revision unsaved and does not claim success', async ({ page }) => {
  await operatorBackend(page, { policyFailure: true });
  await page.goto('/dashboard'); await unlock(page);
  await page.getByRole('button', { name: 'Revise proposal', exact: true }).click();
  await page.getByRole('combobox', { name: 'Primary plan action' }).selectOption('LEAVE_WITH_SECURITY');
  await page.getByRole('textbox', { name: 'Operator reason' }).fill('Review this alternative.');
  await page.getByRole('button', { name: 'Submit revision for customer confirmation' }).click();
  await expect(page.getByRole('alert')).toContainText('The revised proposal violates delivery policy');
  await expect(page.getByText('Revision proposed. A fresh customer confirmation is required.')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Revise the delivery proposal' })).toBeVisible();
});

test('operator review controls and recorded conversation meet automated WCAG AA checks', async ({ page }) => {
  await operatorBackend(page); await page.goto('/dashboard'); await unlock(page);
  await page.getByRole('button', { name: 'Revise proposal', exact: true }).click();
  const report = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
  expect(report.violations).toEqual([]);
});


test('prepared conversations stay in SwiftCart and opening timings remain distinct', async ({ page }) => {
  await mockApi(page, {state:'CALLING'});
  await page.route('**/api/sessions/*/events', route => route.fulfill({json:[{id:1,event_type:'OPENING_LINE_SPOKEN',created_at:session.started_at,payload_json:JSON.stringify({first_audio_ms:650,tts_ms:1200})}]}));
  await page.goto('/dashboard');
  await expect(page.getByRole('status')).toContainText('Customer voice is ready');
  await expect(page.getByRole('link',{name:'Start customer voice demo'})).toHaveCount(0);
  await page.getByText('Performance and audit details',{exact:true}).click();
  await expect(page.getByRole('tabpanel',{name:'Conversation'})).toContainText('650 ms');
  await expect(page.getByRole('tabpanel',{name:'Conversation'})).toContainText('Not available');
});

test('future out-for-delivery orders require an authorized prepare action', async ({ page }) => {
  const requests = await mockApi(page,{sessions:false});
  await page.route('**/api/orders?**', route => route.fulfill({json:[{...order,status:'OUT_FOR_DELIVERY'}]}));
  await page.goto(`/dashboard?order_id=${order.id}`);
  const prepare = page.getByRole('button',{name:'Prepare customer conversation'});
  await expect(prepare).toBeDisabled();
  await unlock(page);
  await expect(prepare).toBeEnabled();
  expect(requests.some(request=>request.method==='POST')).toBe(false);
  await prepare.click();
  await expect.poll(()=>requests.filter(request=>request.path.endsWith('/call')).length).toBe(1);
});

test('pending order selection survives refresh without starting a call', async ({ page }) => {
  const requests = await mockApi(page);
  const pendingId=order.id+1;
  await page.route('**/api/orders?**',route=>route.fulfill({json:[order,{...order,id:pendingId,status:'PENDING'}]}));
  await page.goto(`/dashboard?order_id=${order.id}`);
  await page.getByRole('button',{name:`Order #${pendingId}`,exact:true}).click();
  await page.getByRole('button',{name:/Updated/}).click();
  await expect(page.getByRole('heading',{name:'Awaiting dispatch.'})).toBeVisible();
  await expect(page.getByRole('button',{name:'Mark out for delivery'})).toBeDisabled();
  expect(requests.some(request=>request.method==='POST')).toBe(false);
});
