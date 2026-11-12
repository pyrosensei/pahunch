import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import { mockApi, mockAudio, order, session, planner } from './fixtures';

async function openKeyboardFallback(page: Page) {
  const details = page.locator('details.voice-keyboard-fallback');
  if (!(await details.evaluate(node => node.hasAttribute('open')))) {
    await details.locator('summary').click();
  }
}

for (const legacy of [false, true]) {
  test(`saved neighbour card preserves its recipient (${legacy ? 'legacy' : 'structured'})`, async ({ page }) => {
    const primary = {
      action: 'LEAVE_WITH_NEIGHBOR', window_start: order.window_start,
      window_end: order.window_end, named_recipient: 'Asha', recipient_address: 'Flat 4B',
    };
    const prepared = {
      ...session, state: 'COMMITTED', customer_confirmed: true,
      proposal: { primary, fallback: null },
      courier_card: {
        ...primary, named_recipient: legacy ? null : 'Asha', recipient_address: legacy ? null : 'Flat 4B',
        instructions: ['Leave with the authorized neighbour. Verify OTP directly with the customer.'],
      },
    };
    await mockApi(page);
    await mockAudio(page);
    let saved = false;
    await page.route('**/api/sessions?**', route => route.fulfill({ json: [{
      ...prepared, state: saved ? 'COMMITTED' : 'PENDING_CONFIRMATION',
      verdict: { valid: true, violations: [] }, courier_card: saved ? prepared.courier_card : null,
    }] }));
    await page.route(`**/api/sessions/${session.id}`, route => route.fulfill({ json: {
      ...prepared, state: saved ? 'COMMITTED' : 'PENDING_CONFIRMATION',
      verdict: { valid: true, violations: [] }, courier_card: saved ? prepared.courier_card : null,
    } }));
    await page.route(`**/api/sessions/${session.id}/commit`, route => {
      saved = true;
      return route.fulfill({ json: { state: 'COMMITTED', courier_card: prepared.courier_card } });
    });
    await page.routeWebSocket('**/ws/sessions/**/voice', socket => {
      socket.send(JSON.stringify({ type: 'ready', state: 'PENDING_CONFIRMATION' }));
    });
    await page.goto(`/voice?session_id=${session.id}`);
    await page.getByRole('button', { name: 'Connect conversation' }).click();
    await page.getByRole('button', { name: 'Save delivery plan' }).click();
    const plan = page.getByRole('region', { name: 'Your delivery plan' });
    await expect(plan).toContainText('Asha');
    await expect(plan).toContainText('Flat 4B');
    await expect(plan).toContainText('Saved for the courier.');
  });
}

for (const errorCode of ['LLM_RATE_LIMIT', 'LLM_BILLING_REQUIRED', 'LLM_TIMEOUT', 'UNGROUNDED_PLAN_TEXT']) {
  test(`pending reply and ${errorCode} recovery are visible`, async ({ page }) => {
    await mockApi(page);
    await mockAudio(page);
    let releaseReply: (() => void) | undefined;
    let requests = 0;
    await page.routeWebSocket('**/ws/sessions/**/voice', socket => {
      socket.send(JSON.stringify({ type: 'ready', state: 'DISCOVERY' }));
      socket.onMessage(raw => {
        if (typeof raw !== 'string') return;
        const message = JSON.parse(raw);
        if (message.type !== 'scripted_turn') return;
        requests++;
        const generation = requests;
        releaseReply = () => {
          socket.send(JSON.stringify({
            type: 'turn', generation_id: generation, state: 'DISCOVERY',
            transcript: message.text, agent_message: generation === 1 ? 'Please try again later.' : 'Yes, I can hear you.',
            turn: { customer_confirmed: false },
            planner: { ...planner, model: 'synthetic-planner-model', degraded: generation === 1, error_code: generation === 1 ? errorCode : null },
          }));
          socket.send(JSON.stringify({ type: 'agent_audio_end', generation_id: generation }));
        };
      });
    });
    await page.goto(`/voice?order_id=${order.id}`);
    await page.getByRole('button', { name: 'Connect conversation' }).click();
    await openKeyboardFallback(page);
    const input = page.getByRole('textbox', { name: 'Your reply' });
    const send = page.getByRole('button', { name: 'Send message' });
    await input.fill('Can you hear me?');
    await send.click();
    await expect(input).toBeDisabled();
    await expect(send).toBeDisabled();
    await expect(page.getByRole('heading', { name: 'Preparing your reply' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Save delivery plan' })).toBeDisabled();
    await expect.poll(() => requests).toBe(1);
    releaseReply!();
    await expect(page.locator('.voice-provider-notice')).toContainText(errorCode);
    await expect(input).toBeEnabled();
    await page.getByText('Conversation diagnostics', { exact: true }).click();
    await expect(page.locator('.voice-metrics')).toContainText('synthetic-planner-model');
    await expect(page.locator('.voice-metrics')).toContainText(errorCode);
    await input.fill('Try again');
    await send.click();
    await expect.poll(() => requests).toBe(2);
    releaseReply!();
    await expect(page.locator('.voice-provider-notice')).toHaveCount(0);
    await expect(page.getByRole('log')).toContainText('Yes, I can hear you.');
  });
}

test('policy rejection, changed confirmation, degraded audio and a failed commit stay reviewable', async ({ page }, testInfo) => {
  await mockApi(page);
  await mockAudio(page);
  let current: Record<string, unknown> = { ...session };
  let generation = 0;
  let commitCount = 0;
  const commitKeys: unknown[] = [];
  await page.route('**/api/sessions?**', route => route.fulfill({ json: [current] }));
  await page.route('**/api/sessions/*/commit', route => {
    commitCount++;
    commitKeys.push(route.request().postDataJSON().idempotency_key);
    return route.fulfill(commitCount === 1 ? { status: 409, json: { detail: 'The delivery window changed. Review and confirm the current plan.' } } : { json: { state: 'COMMITTED', courier_card: { action: 'HAND_TO_CUSTOMER', window_start: order.window_start, window_end: order.window_end, call_on_arrival: true, instructions: ['Hand directly to the customer. Call on arrival.'] } } });
  });
  await page.routeWebSocket('**/ws/sessions/**/voice', socket => {
    setTimeout(() => socket.send(JSON.stringify({ type: 'ready', state: 'DISCOVERY' })), 25);
    socket.onMessage(raw => {
      if (typeof raw !== 'string') return;
      const message = JSON.parse(raw);
      if (message.type !== 'scripted_turn') return;
      const rejected = message.text === 'Leave with security';
      const confirmed = message.text === 'Yes';
      current = { ...current, state: rejected ? 'NEGOTIATION' : 'PENDING_CONFIRMATION', customer_confirmed: confirmed,
        proposal: { primary: { action: rejected ? 'LEAVE_WITH_SECURITY' : 'HAND_TO_CUSTOMER', window_start: order.window_start, window_end: order.window_end } },
        verdict: { valid: !rejected, violations: rejected ? [{ code: 'COD_REQUIRES_DIRECT_HANDOFF' }] : [] } };
      generation++;
      socket.send(JSON.stringify({ type: 'turn', generation_id: generation, state: current.state,
        transcript: message.text, agent_message: rejected ? 'Cash on delivery requires a direct handoff. Can we arrange that?' : confirmed ? 'Your current plan is confirmed.' : 'Direct handoff is allowed. Please confirm this updated plan.',
        turn: { customer_confirmed: confirmed, verdict: current.verdict }, planner: { ...planner, wording_source: 'template', fallback_reason: 'PROVIDER_UNAVAILABLE' } }));
      socket.send(JSON.stringify({ type: 'agent_audio_end', generation_id: generation, audio_error: true }));
    });
  });
  await page.goto(`/voice?order_id=${order.id}`);
  await page.getByRole('button', { name: 'Connect conversation' }).click();
  const send = async (text: string) => { await openKeyboardFallback(page); await page.getByRole('textbox', { name: 'Your reply' }).fill(text); await page.getByRole('button', { name: 'Send message' }).click(); await expect(page.getByRole('log')).toContainText(text); };
  const save = page.getByRole('button', { name: 'Save delivery plan' });
  await send('Leave with security');
  await expect(page.locator('.voice-verdict')).toContainText('cod requires direct handoff');
  await expect(save).toBeDisabled();
  await expect(page.getByRole('alert')).toContainText('Your reply is available as text');
  await page.getByText('Conversation diagnostics', { exact: true }).click();
  await expect(page.locator('.voice-metrics')).toContainText('PROVIDER_UNAVAILABLE');
  await page.screenshot({ path: testInfo.outputPath('voice-policy-rejected-degraded.png'), fullPage: true });
  await send('Hand directly to me');
  await send('Yes');
  await expect(save).toBeEnabled();
  await send('Change the plan');
  await expect(save).toBeDisabled();
  expect(commitCount).toBe(0);
  await send('Yes');
  await save.click();
  await expect(page.getByRole('alert')).toContainText('delivery window changed');
  await expect(page.getByRole('button', { name: 'Plan saved' })).toHaveCount(0);
  await send('Review the new window');
  await expect(save).toBeDisabled();
  await send('Yes');
  await save.click();
  await expect(page.getByRole('button', { name: 'Plan saved' })).toBeDisabled();
  await expect(page.getByRole('button', { name: 'Start microphone' })).toBeDisabled();
  await expect(page.getByRole('textbox', { name: 'Your reply' })).toBeDisabled();
  await expect(page.locator('.voice-plan-card')).toContainText('not been marked delivered');
  expect(commitCount).toBe(2);
  expect(commitKeys[0]).toBe(commitKeys[1]);
  await page.screenshot({ path: testInfo.outputPath('voice-committed.png'), fullPage: true });
});

for (const variant of [{ transport: 'twilio' as const, state: 'DISCOVERY' }, { transport: 'browser' as const, state: 'COMMITTED' }]) {
  test(`${variant.transport} ${variant.state} session stays read-only`, async ({ page }) => {
    await mockApi(page, variant);
    await mockAudio(page);
    let sockets = 0;
    await page.routeWebSocket('**/ws/**', () => { sockets++; });
    await page.goto(`/voice?session_id=${session.id}&order_id=${order.id}`);
    await page.getByRole('button', { name: 'Connect conversation' }).click();
    await expect(page.getByRole('alert')).toBeVisible();
    await openKeyboardFallback(page);
    await expect(page.getByRole('textbox', { name: 'Your reply' })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Start microphone' })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Save delivery plan' })).toBeDisabled();
    expect(sockets).toBe(0);
  });
}
