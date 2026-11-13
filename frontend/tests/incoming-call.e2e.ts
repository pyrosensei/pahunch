import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { mockApi, order, session } from './fixtures';

async function ringingCustomer(page: Parameters<typeof mockApi>[0]) {
  await mockApi(page, {state:'CALLING'});
  await page.route(`**/api/orders/${order.id}`, route => route.fulfill({json:{...order,status:'OUT_FOR_DELIVERY'}}));
}

test('incoming call follows the customer from their order to the landing page', async ({page}) => {
  await ringingCustomer(page);
  await page.goto(`/shop?order_id=${order.id}`);
  await expect(page.getByRole('dialog', {name:'SwiftCart is calling.'})).toBeVisible();
  await page.getByRole('button', {name:'Not now',exact:true}).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('link', {name:'Answer call',exact:true})).toBeVisible();
  await page.goto('/');
  const dialog = page.getByRole('dialog', {name:'SwiftCart is calling.'});
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(`Order #${order.id}`);
  await expect(page.locator(':focus')).toHaveText('Accept call');
  const report = await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
  expect(report.violations).toEqual([]);
});

test('operator and unselected or closed customer orders never receive a call prompt', async ({page}) => {
  await ringingCustomer(page);
  await page.goto('/');
  await expect(page.getByRole('heading', {level:1})).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.goto(`/shop?order_id=${order.id}`);
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.goto(`/dashboard?order_id=${order.id}`);
  await expect(page.getByRole('heading', {level:1})).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.route(`**/api/orders/${order.id}`, route => route.fulfill({json:{...order,status:'CANCELLED'}}));
  await page.goto(`/shop?order_id=${order.id}`);
  await expect(page.getByRole('heading', {name:'Cancelled',level:1})).toBeVisible();
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('call page verifies the owning order and blocks closed or phone sessions', async ({page}) => {
  await mockApi(page, {state:'CALLING'});
  await page.route(`**/api/orders/${order.id}`, route => route.fulfill({json:{...order,status:'CANCELLED'}}));
  let sockets=0;
  await page.routeWebSocket('**/ws/sessions/**/voice', () => {sockets++;});
  await page.goto(`/call?session_id=${session.id}&order_id=${order.id+1}`);
  await expect(page.getByRole('alert')).toContainText('This order is closed');
  expect(sockets).toBe(0);
  await page.route(`**/api/sessions/${session.id}`, route => route.fulfill({json:{...session,transport:'twilio'}}));
  await page.reload();
  await expect(page.getByRole('alert')).toContainText('different calling channel');
  expect(sockets).toBe(0);
});

test('failed incoming-call refresh removes the invitation', async ({page}) => {
  await ringingCustomer(page);
  await page.goto(`/shop?order_id=${order.id}`);
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.route('**/api/sessions?**', route => route.fulfill({status:503,json:{detail:'Unavailable'}}));
  await expect(page.getByRole('dialog')).toHaveCount(0);
});
