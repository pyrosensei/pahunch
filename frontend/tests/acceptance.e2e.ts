import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mockApi, mockAudio, order, planner, session } from './fixtures';

async function mockConversation(page: Page) {
  const sent: Array<Record<string, unknown>> = [];
  let connected = 0;
  let closed = 0;
  let generation = 0;
  await page.routeWebSocket('**/ws/sessions/**/voice', socket => {
    connected++;
    setTimeout(() => socket.send(JSON.stringify({ type: 'ready', state: 'DISCOVERY' })), 30);
    socket.onClose(() => { closed++; });
    socket.onMessage(raw => {
      if (typeof raw !== 'string') return;
      const event = JSON.parse(raw) as Record<string, unknown>;
      sent.push(event);
      if (event.type !== 'scripted_turn') return;
      const confirmation = String(event.text).toLowerCase() === 'yes';
      generation++;
      socket.send(JSON.stringify({ type: 'turn', generation_id: generation,
        state: 'PENDING_CONFIRMATION',
        transcript: confirmation ? 'Yes' : 'Please hand the parcel directly to me.',
        agent_message: confirmation ? 'Thank you. I will submit the plan for a final check.' : 'Direct handoff during your delivery window. May I take this plan forward?',
        turn: { customer_confirmed: confirmation, verdict: { valid: true, violations: [] } },
        planner: { ...planner, action: confirmation ? 'CONFIRM_PLAN' : 'PROPOSE_PLAN' },
        timings: { stt_ms: 0, planner_ms: 20, tts_ms: 0, first_audio_ms: 50 },
      }));
      socket.send(JSON.stringify({ type: 'agent_audio_end', generation_id: generation,
        timings: { planner_ms: 20, first_audio_ms: 50 } }));
    });
  });
  return { sent, connections: () => connected, closed: () => closed };
}

async function openKeyboardFallback(page: Page) {
  const details = page.locator('details.voice-keyboard-fallback');
  if (!(await details.evaluate(node => node.hasAttribute('open')))) {
    await details.locator('summary').click();
  }
}

test('landing works without WebGL, respects reduced motion, and exposes keyboard navigation', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.addInitScript(() => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, kind: string, ...args: unknown[]) {
      if (kind === 'webgl' || kind === 'webgl2' || kind === 'experimental-webgl') return null;
      return original.apply(this, [kind, ...args] as never);
    } as typeof original;
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('A delivery conversation.');
  await expect(page.getByRole('link', { name: 'Try a delivery' }).first()).toBeVisible();
  await expect(page.locator('canvas')).toHaveCount(0);
  await expect(page.getByLabel('Parcel illustration')).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#main$/);
});

test('WebGL failure preserves the landing illustration and primary action', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.addInitScript(() => {
    const counters = { attempts: 0 };
    Object.defineProperty(window, '__webgl', { value: counters });
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function (this: HTMLCanvasElement, kind: string, ...args: unknown[]) {
      if (kind === 'webgl' || kind === 'webgl2' || kind === 'experimental-webgl') {
        counters.attempts++;
        return null;
      }
      return original.apply(this, [kind, ...args] as never);
    } as typeof original;
  });
  await page.goto('/');
  await expect.poll(() => page.evaluate(() => (window as unknown as { __webgl: { attempts: number } }).__webgl.attempts), { timeout: 15_000 }).toBeGreaterThan(0);
  await expect(page.getByLabel('Parcel illustration')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Try a delivery' }).first()).toBeEnabled();
});

test('voice connects only on request and saves a plan only after backend confirmation', async ({ page }) => {
  const requests = await mockApi(page);
  await mockAudio(page);
  const wire = await mockConversation(page);
  await page.goto(`/voice?order_id=${order.id}`);
  await expect(page.getByRole('button', { name: 'Connect conversation' })).toBeEnabled();
  expect(wire.connections()).toBe(0);
  await expect(page.getByRole('button', { name: 'Save delivery plan' })).toBeDisabled();
  await page.getByRole('button', { name: 'Connect conversation' }).click();
  await expect(page.getByRole('button', { name: 'Disconnect', exact: true })).toBeVisible();
  expect(wire.connections()).toBe(1);
  expect(wire.sent.find(message => message.type === 'config')).toMatchObject({
    tts_sample_rate: 16000, playback_tracking: true, audio_feedback: true,
  });
  await expect(page.locator('canvas')).toHaveCount(0);
  await openKeyboardFallback(page);
  await page.getByRole('textbox', { name: 'Your reply' }).fill('Please hand the parcel directly to me.');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(page.getByRole('log')).toContainText('May I take this plan forward?');
  await expect(page.getByRole('button', { name: 'Save delivery plan' })).toBeDisabled();
  expect(requests.filter(request => request.path.endsWith('/commit'))).toHaveLength(0);
  await page.getByRole('textbox', { name: 'Your reply' }).fill('Yes');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(page.getByRole('button', { name: 'Save delivery plan' })).toBeEnabled();
  await page.getByRole('button', { name: 'Save delivery plan' }).click();
  await expect(page.getByRole('button', { name: 'Plan saved' })).toBeDisabled();
  await expect(page.getByRole('heading', { name: 'All set for the courier.' })).toBeVisible();
  const commits = requests.filter(request => request.path.endsWith('/commit'));
  expect(commits).toHaveLength(1);
  expect((commits[0].body as { idempotency_key: string }).idempotency_key).toContain(session.id);
});

test('SwiftCart product selection derives delivery verification from payment without an OTP checkbox', async ({ page }) => {
  await mockApi(page);
  await page.goto('/shop');
  await expect(page.getByRole('button', { name: /^Choose / })).toHaveCount(4);
  await expect(page.getByRole('button', { name: 'Choose Studio Wireless Headphones' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Choose Everyday Canvas Backpack' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Choose Pulse Smartwatch' })).toBeVisible();
  await expect(page.getByText('No OTP required', { exact: true })).toBeVisible();
  await expect(page.getByRole('checkbox')).toHaveCount(0);
  await page.getByRole('button', { name: 'Prepaid', exact: true }).click();
  await expect(page.getByText('OTP required at delivery', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Cash on delivery', exact: true }).click();
  await expect(page.getByText('No OTP required', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Choose Airbook 14 Laptop' }).click();
  await expect(page.getByText(/This laptop requires direct handoff/)).toBeVisible();
});

test('SwiftCart checkout keeps dispatch and operator controls out of the customer page', async ({ page }) => {
  const requests = await mockApi(page, { sessions:false });
  await page.goto('/shop');
  await page.getByRole('button', { name: 'Choose Airbook 14 Laptop' }).click();
  await page.getByRole('button', { name: 'Prepaid', exact: true }).click();
  await page.getByRole('button', { name: 'Place demo order' }).click();
  await expect(page).toHaveURL(new RegExp(`/shop\\?order_id=${order.id}$`));
  await expect(page.getByRole('heading', { name: 'We’re getting your order ready.' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Mark out for delivery' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Connect conversation' })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Dashboard', exact: true })).toHaveCount(0);
  expect(requests.find(request => request.path === '/api/orders' && request.method === 'POST')?.body).toEqual({payment_mode:'PREPAID',product_id:'laptop'});
  expect(requests.some(request => request.path.endsWith('/call') || request.path.endsWith('/out-for-delivery'))).toBe(false);
});

test('checkout failure has an actionable error without order success', async ({ page }) => {
  await mockApi(page);
  await page.route('**/api/orders',route=>route.request().method()==='POST'?route.fulfill({status:503,json:{detail:'Demo backend unavailable'}}):route.fallback());
  await page.goto('/shop');
  await page.getByRole('button', { name: 'Place demo order' }).click();
  await expect(page.getByRole('alert')).toContainText('Demo backend unavailable');
  await expect(page.getByRole('button', { name: 'Place demo order' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Mark out for delivery' })).toHaveCount(0);
});

test('a phone-owned session never opens a browser customer socket', async ({ page }) => {
  await mockApi(page, { transport: 'twilio' });
  await page.goto('/shop');
  await page.getByRole('button', { name: 'Place demo order' }).click();
  await expect(page.getByRole('button', { name: 'Connect conversation' })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Start PC voice demo' })).toHaveCount(0);
});

test('dashboard escapes transcript markup and preserves data with a stale warning on refresh failure', async ({ page }) => {
  const options = { failure: false, transcript: '<img src=x onerror="alert(1)"> A safe text transcript.' };
  await mockApi(page, options);
  await page.goto('/dashboard');
  const transcript = page.getByRole('tabpanel', { name: 'Conversation', exact: true });
  await expect(transcript).toContainText(options.transcript);
  await expect(transcript.locator('img')).toHaveCount(0);
  await transcript.getByText('Response details').click();
  await expect(transcript).toContainText('Wording: Model');
  options.failure = true;
  await page.getByRole('button', { name: /^Updated / }).click();
  await expect(page.getByRole('alert')).toContainText(/stale/i);
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await expect(page.getByRole('button', { name: /Order #1050/ })).toBeVisible();
  await expect(transcript).toContainText(options.transcript);
});

test('denied microphone keeps typed replies available and never displays unredacted typed text', async ({ page }) => {
  await mockApi(page);
  await mockAudio(page);
  await mockConversation(page);
  await page.addInitScript(() => {
    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
      value: async () => { throw new DOMException('Permission denied', 'NotAllowedError'); },
    });
  });
  await page.goto(`/voice?order_id=${order.id}`);
  await page.getByRole('button', { name: 'Connect conversation' }).click();
  await page.getByRole('button', { name: 'Start microphone' }).click();
  await expect(page.getByRole('alert')).toContainText(/microphone access is unavailable/i);
  await openKeyboardFallback(page);
  await page.getByRole('textbox', { name: 'Your reply' }).fill('My OTP is 123456');
  await page.getByRole('button', { name: 'Send message' }).click();
  await expect(page.getByRole('textbox', { name: 'Your reply' })).toHaveValue('');
  await expect(page.getByRole('log')).not.toContainText('123456');
  await expect(page.getByRole('log')).toContainText('Please hand the parcel directly to me.');
});

test('leaving the voice route closes the socket and releases microphone tracks', async ({ page }) => {
  await mockApi(page);
  await mockAudio(page);
  const wire = await mockConversation(page);
  await page.addInitScript(() => {
    const counters = { starts: 0, stops: 0 };
    Object.defineProperty(window, '__micCounters', { value: counters });
    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
      value: async () => {
        counters.starts++;
        return { getTracks: () => [{ stop: () => { counters.stops++; } }] };
      },
    });
  });
  await page.goto(`/voice?order_id=${order.id}`);
  await page.getByRole('button', { name: 'Connect conversation' }).click();
  await page.getByRole('button', { name: 'Start microphone' }).click();
  await expect(page.getByRole('button', { name: 'Stop microphone' })).toBeVisible();
  await expect(page.locator('canvas')).toHaveCount(0);
  await page.getByRole('link', { name: 'SwiftCart shop', exact: true }).click();
  await expect.poll(() => wire.closed()).toBe(1);
  await expect.poll(() => page.evaluate(() => (window as unknown as { __micCounters: { stops: number } }).__micCounters.stops)).toBe(1);
});

for (const route of ['/', '/shop', '/dashboard', '/voice']) {
  test(`WCAG AA accessibility on ${route}`, async ({ page }) => {
    await mockApi(page);
    await page.goto(route);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await page.evaluate(() => document.fonts.ready);
    const report = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
    expect(report.violations).toEqual([]);
  });

  test(`responsive ${route} layout and visual checkpoint`, async ({ page }, testInfo) => {
    await mockApi(page);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto(route);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.locator('main')).toBeVisible();
    const widths = await page.evaluate(() => ({ content: document.documentElement.scrollWidth, viewport: innerWidth }));
    expect(widths.content).toBeLessThanOrEqual(widths.viewport + 1);
    const path = testInfo.outputPath(`${route === '/' ? 'landing' : route.slice(1)}.png`);
    await page.screenshot({ path: testInfo.outputPath(`${route === '/' ? 'landing' : route.slice(1)}-viewport.png`), animations: 'disabled' });
    await page.screenshot({ path, fullPage: true, animations: 'disabled' });
    await testInfo.attach('Page screenshot', { path, contentType: 'image/png' });
  });
}
