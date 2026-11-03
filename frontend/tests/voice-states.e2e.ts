import { test, expect } from '@playwright/test';
import { mockApi, mockAudio, order, session, planner } from './fixtures';

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
  const send = async (text: string) => { await page.getByRole('textbox', { name: 'Your reply' }).fill(text); await page.getByRole('button', { name: 'Send message' }).click(); await expect(page.getByRole('log')).toContainText(text); };
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
    await expect(page.getByRole('textbox', { name: 'Your reply' })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Start microphone' })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Save delivery plan' })).toBeDisabled();
    expect(sockets).toBe(0);
  });
}
