import { chromium, expect } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

// Opt in against an isolated local backend. This creates one synthetic order;
// it never connects a voice socket, requests a microphone, or invokes a provider.
if (!process.argv.includes('--isolated-backend')) throw new Error('Use --isolated-backend with a disposable local database and browser transport.');
const baseURL = 'http://127.0.0.1:5173';
const output = resolve('.artifacts/editorial-review');
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
const context = await browser.newContext({ baseURL, reducedMotion: 'reduce' });
const page = await context.newPage();
const report = [];
try {
  const scheduler = await (await page.request.get('/api/dialer/status')).json();
  if (scheduler.transport !== 'browser') throw new Error('Browser transport is required for the local review.');
  for (const [width, height] of [[1440, 1000], [768, 1024], [390, 844]]) {
    await page.setViewportSize({ width, height });
    for (const route of ['/', '/shop', '/dashboard', '/voice']) {
      await page.goto(route);
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      await page.evaluate(() => document.fonts.ready);
      if (route === '/voice') await expect(page.locator('#voice-order')).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      expect(overflow).toBeLessThanOrEqual(1);
      await expect(page.getByRole('alert')).toHaveCount(0);
      const file = `${width}-${route.slice(1) || 'landing'}.png`;
      await page.screenshot({ path: resolve(output, file), fullPage: true, animations: 'disabled', scale: 'css' });
      await page.screenshot({ path: resolve(output, `${width}-${route.slice(1) || 'landing'}-viewport.png`), animations: 'disabled', scale: 'css' });
      report.push({ width, height, route, screenshot: file, overflow });
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/shop');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/orders') && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Place demo order' }).click();
  const response = await responsePromise;
  expect(response.status()).toBe(201);
  const order = await response.json();
  expect(order.status).toBe('PENDING');
  await expect(page.getByRole('button', { name: 'Mark out for delivery' })).toBeEnabled();
  await page.screenshot({ path: resolve(output, 'real-order-placed.png'), fullPage: true });
  await page.getByRole('button', { name: 'Mark out for delivery' }).click();
  await expect(page.getByRole('link', { name: 'Open conversation', exact: true })).toBeVisible({ timeout: 15000 });
  await expect(page.locator('.order-facts')).toContainText('Out for delivery');
  await page.screenshot({ path: resolve(output, 'real-order-ready.png'), fullPage: true });
  await page.getByRole('link', { name: 'View order in dashboard' }).click();
  await expect(page.getByRole('button', { name: new RegExp(`Order #${order.id}`) })).toBeVisible();
  await page.getByRole('link', { name: 'Open conversation', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Connect conversation' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Save delivery plan' })).toBeDisabled();
  await expect(page.locator('[data-voice-state]')).toHaveAttribute('data-voice-state', 'idle');
  report.push({ journey: 'Real local checkout → separate readiness → scheduler session → dashboard → voice (not connected)', orderId: order.id });
  console.log(JSON.stringify(report, null, 2));
} finally {
  await writeFile(resolve(output, 'review.json'), JSON.stringify(report, null, 2));
  await browser.close();
}
