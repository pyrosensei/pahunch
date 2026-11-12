import { test, expect } from '@playwright/test';
import { mockApi, mockAudio } from './fixtures';

test('checkout stays inside its card and order tracking survives reload', async ({ page }) => {
  const requests = await mockApi(page);
  await page.goto('/shop');
  const summary = page.locator('.checkout-product');
  const withinCard = await summary.evaluate(node => {
    const card = node.getBoundingClientRect();
    return [...node.children].every(child => child.getBoundingClientRect().right <= card.right + 1);
  });
  expect(withinCard).toBe(true);
  await page.getByRole('button', { name: 'Place demo order' }).click();
  await expect(page.getByRole('region',{name:'Your order details'})).toContainText('#1050');
  await page.reload();
  await expect(page.getByRole('region',{name:'Your order details'})).toContainText('#1050');
  expect(requests.filter(request => request.method === 'POST')).toHaveLength(1);
});

test('a tracking refresh failure remains visible without enabling dispatch', async ({ page }) => {
  await mockApi(page);
  await page.route('**/api/sessions?**', route => route.fulfill({status:503,json:{detail:'Unavailable'}}));
  await page.goto('/shop?order_id=1050');
  await expect(page.getByRole('status')).toContainText('Tracking could not refresh');
  await expect(page.getByRole('button',{name:'Connect conversation'})).toHaveCount(0);
  await expect(page.getByRole('button',{name:'Mark out for delivery'})).toHaveCount(0);
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
