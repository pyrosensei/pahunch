import { test, expect } from '@playwright/test';
import { mockApi, mockAudio } from './fixtures';

for (const transport of ['browser', 'twilio'] as const) {
  test(`checkout routes an active ${transport} conversation to its supported interface`, async ({ page }) => {
    const requests = await mockApi(page, { transport });
    await page.goto('/shop');
    await page.getByRole('button', { name: 'Place demo order' }).click();
    await page.getByRole('button', { name: 'Mark out for delivery' }).click();
    const launch = page.getByRole('link', { name: 'Start PC voice demo' });
    if (transport === 'twilio') {
      await expect(page.getByRole('status')).toContainText('A phone conversation is active');
      await expect(launch).toHaveCount(0);
    } else {
      await expect(launch).toHaveAttribute('href', /session_id=browser-test-session/);
    }
    await expect(page.getByRole('link', { name: 'Monitor delivery' })).toBeVisible();
    expect(requests.filter(request => request.path.endsWith('/call'))).toHaveLength(0);
  });
}

test('checkout waits for session ownership and keeps monitoring available after a lookup failure', async ({ page }) => {
  await mockApi(page);
  let finishLookup: () => void = () => {};
  const lookupPending = new Promise<void>(resolve => { finishLookup = resolve; });
  await page.route('**/api/sessions?**', async route => {
    await lookupPending;
    await route.fulfill({ status: 503, json: { detail: 'Session lookup unavailable' } });
  });
  await page.goto('/shop');
  await page.getByRole('button', { name: 'Place demo order' }).click();
  await page.getByRole('button', { name: 'Mark out for delivery' }).click();
  await expect(page.getByRole('link', { name: 'Start PC voice demo' })).toHaveCount(0);
  finishLookup();
  await expect(page.getByRole('status')).toContainText('Connection interrupted');
  await expect(page.getByRole('link', { name: 'Start PC voice demo' })).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Monitor delivery' })).toBeVisible();
});

test('checkout stays inside its card and advances the visible delivery steps', async ({ page }) => {
  await mockApi(page);
  await page.goto('/shop');
  await expect(page.getByRole('button', { name: 'Place demo order' })).toBeEnabled();
  const summary = page.locator('.checkout-product');
  const withinCard = await summary.evaluate(node => {
    const card = node.getBoundingClientRect();
    return [...node.children].every(child => child.getBoundingClientRect().right <= card.right + 1);
  });
  expect(withinCard).toBe(true);
  const progress = page.getByRole('list', { name: 'Demo delivery steps' });
  await expect(progress.locator('[aria-current="step"]')).toHaveText('1Choose your item');
  await page.getByRole('button', { name: 'Place demo order' }).click();
  await expect(progress.locator('[aria-current="step"]')).toContainText('Prepare delivery');
  await page.getByRole('button', { name: 'Mark out for delivery' }).click();
  await expect(progress.locator('[aria-current="step"]')).toContainText('Follow the conversation');
  await expect(page.getByRole('link', { name: 'Monitor delivery' })).toHaveClass(/primary/);
});

test('voice controls precede the transcript and delivery details remain accessible', async ({ page }, testInfo) => {
  await mockApi(page); await mockAudio(page);
  await page.routeWebSocket('**/ws/sessions/**/voice', socket => {
    setTimeout(() => socket.send(JSON.stringify({ type: 'ready', state: 'DISCOVERY' })), 30);
  });
  await page.goto('/voice');
  await page.getByText('Payment & delivery details', { exact: true }).click();
  await expect(page.locator('.voice-order-facts')).toBeVisible();
  await page.getByText('Payment & delivery details', { exact: true }).click();
  await page.getByRole('button', { name: 'Connect conversation' }).click();
  const mic = page.getByRole('button', { name: 'Start microphone' });
  await expect(mic).toBeEnabled();
  const button = await mic.boundingBox();
  const transcript = await page.getByRole('log').boundingBox();
  expect(button!.height).toBeGreaterThanOrEqual(44);
  expect(button!.y + button!.height).toBeLessThan(transcript!.y);
  if (testInfo.project.name === 'mobile') {
    await page.locator('.voice-thread').evaluate(node => window.scrollTo(0, node.getBoundingClientRect().top + scrollY - 310));
    await expect(mic).toBeInViewport();
    await page.screenshot({ path: testInfo.outputPath('mobile-active-controls.png') });
  }
  await page.getByRole('link', { name: /^View delivery plan/ }).click();
  await expect(page.getByRole('region', { name: 'Your delivery plan', exact: true })).toBeInViewport();
  await expect(page.getByRole('button', { name: 'Save delivery plan' })).toBeDisabled();
});
