import { test, expect } from '@playwright/test';
import { mockApi, order, session } from './fixtures';

test('one landing canvas follows native scroll, pauses offscreen, and recovers from context loss', async ({ page }, testInfo) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto('/');
  const canvas = page.locator('canvas');
  await expect(canvas).toHaveCount(1);
  await expect(page.locator('.delivery-diorama')).toHaveAttribute('data-phase', '0');
  await page.screenshot({ path: testInfo.outputPath('hero-webgl.png'), animations: 'disabled' });
  for (const phase of [1, 2, 3]) {
    await page.locator(`[data-chapter="${phase}"]`).scrollIntoViewIfNeeded();
    await expect(page.locator('.delivery-diorama')).toHaveAttribute('data-phase', String(phase));
    await expect(canvas).toHaveCount(1);
    await page.screenshot({ path: testInfo.outputPath(`story-${phase}.png`), animations: 'disabled' });
  }
  await expect(page.locator('.illustrative-courier')).toContainText('Explicitly confirmed');
  await page.locator('.demo-invitation').scrollIntoViewIfNeeded();
  await expect(page.locator('.delivery-diorama')).toHaveAttribute('data-rendering', 'paused');
  await page.locator('#hero-title').scrollIntoViewIfNeeded();
  await page.locator('.delivery-diorama').scrollIntoViewIfNeeded();
  await expect(page.locator('.delivery-diorama')).toHaveAttribute('data-rendering', 'active');
  await page.getByRole('button', { name: 'Pause decorative motion' }).click();
  await expect(page.locator('.delivery-diorama')).toHaveAttribute('data-rendering', 'paused');
  await page.getByRole('button', { name: 'Enable decorative motion' }).click();
  await canvas.evaluate(node => node.dispatchEvent(new Event('webglcontextlost', { cancelable: true })));
  await expect(canvas).toHaveCount(0);
  await expect(page.getByLabel('Parcel illustration')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Try a delivery' }).first()).toBeEnabled();
});

test('document visibility suspends the scene and reduced-motion changes replace it with SVG', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto('/');
  await page.locator('.delivery-diorama').scrollIntoViewIfNeeded();
  await expect(page.locator('canvas')).toHaveCount(1);
  await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => true }); document.dispatchEvent(new Event('visibilitychange')); });
  await expect(page.locator('.delivery-diorama')).toHaveAttribute('data-rendering', 'paused');
  await page.evaluate(() => { Object.defineProperty(document, 'hidden', { configurable: true, get: () => false }); document.dispatchEvent(new Event('visibilitychange')); });
  await expect(page.locator('.delivery-diorama')).toHaveAttribute('data-rendering', 'active');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await expect(page.locator('canvas')).toHaveCount(0);
  await expect(page.getByLabel('Parcel illustration')).toBeVisible();
  await expect(page.locator('[data-chapter]')).toHaveCount(3);
});

for (const route of ['/shop', '/dashboard', '/voice']) {
  test(`${route} loads without Three.js or a decorative canvas`, async ({ page }) => {
    await mockApi(page);
    const requested: string[] = [];
    page.on('request', request => requested.push(request.url()));
    await page.goto(route);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    expect(requested.filter(url => /ParcelScene|react-three|three\.module/.test(url))).toEqual([]);
    await expect(page.locator('canvas')).toHaveCount(0);
    if (route === '/voice') {
      await expect(page.locator('[data-voice-state]')).toHaveAttribute('data-voice-state', 'idle');
      await expect(page.locator('meter')).toHaveCount(0);
      await expect(page.locator('.voice-wave')).toHaveCount(0);
    }
  });
}

test('dashboard displays substantive reply timings and keeps missing values unavailable', async ({ page }, testInfo) => {
  await mockApi(page, { transcript: 'I will be home this evening. Please hand it directly to me.' });
  await page.route('**/api/sessions/*/events', route => route.fulfill({ json: [{ id: 1, event_type: 'VOICE_TURN', created_at: session.started_at, payload_json: JSON.stringify({ stt_ms: null, planner_ms: 135, tts_ms: 780, total_ms: 920, first_audio_ms: 65, first_response_audio_ms: 480, ack_played: true }) }] }));
  await page.route('**/api/sessions?**', route => route.fulfill({ json: [{ ...session, state: 'PENDING_CONFIRMATION', customer_confirmed: true, proposal: { primary: { action: 'HAND_TO_CUSTOMER', window_start: order.window_start, window_end: order.window_end, call_on_arrival: true } }, verdict: { valid: true, violations: [] } }] }));
  await page.goto('/dashboard');
  await expect(page.getByText('Customer confirmed · not yet committed')).toBeVisible();
  await expect(page.locator('.plan-progress').getByText('Committed')).toContainText('pending');
  await page.getByRole('tab', { name: 'Performance' }).click();
  const panel = page.getByRole('tabpanel', { name: 'Performance' });
  await expect(panel).toContainText('920 ms');
  await expect(panel).toContainText('480 ms');
  await expect(panel).not.toContainText('ACKNOWLEDGEMENT AUDIO');
  await expect(panel).not.toContainText('65 ms');
  await expect(panel.locator('.metric-bar').first()).toContainText('Not available');
  await expect(panel.locator('.metric-bar').first().locator('.bar-track>span')).toHaveCSS('width', '0px');
  await page.screenshot({ path: testInfo.outputPath('dashboard-confirmed-performance.png'), fullPage: true });
});

test('legacy root forwarding preserves session and filter query parameters', async ({ page }) => {
  await mockApi(page);
  await page.goto(`/?order_id=${order.id}&session_id=${session.id}&tab=events&filter=a%2Fb`);
  await expect(page).toHaveURL(`/dashboard?order_id=${order.id}&session_id=${session.id}&tab=events&filter=a%2Fb`);
});
