import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import { mockAudio, order, session } from './fixtures';

const resolvedOrder = { ...order, id: 1042, status: 'OUT_FOR_DELIVERY' };
const activeOrder = { ...order, id: 1050, status: 'OUT_FOR_DELIVERY' };
const activeId = 'active-browser-1050';

async function mockSelectionBackend(page: Page) {
  const requests: string[] = [];
  const calls = [
    { ...session, id: 'committed-1042', order_id: 1042, state: 'COMMITTED' },
    { ...session, id: activeId, order_id: 1050, state: 'CALLING' },
  ];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    requests.push(`${request.method()} ${path}`);
    const fulfill = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/api/orders') return fulfill([resolvedOrder, activeOrder]);
    if (path === '/api/dialer/status') return fulfill({
      running: true, transport: 'browser', orders: [
        { order_id: 1042, skip_reason: 'RESOLVED' },
        { order_id: 1050, skip_reason: 'ACTIVE_CALL' },
      ],
    });
    if (path === '/api/sessions') {
      const orderId = Number(url.searchParams.get('order_id'));
      return fulfill(calls.filter(call => (!orderId || call.order_id === orderId)
        && (url.searchParams.get('active') !== 'true' || call.state !== 'COMMITTED')));
    }
    if (path === '/api/orders/1042/active-call') return fulfill({ detail: 'No active call for this order' }, 404);
    if (path === '/api/orders/1042/call') return fulfill({ detail: 'A delivery plan is already committed for this order.' }, 409);
    if (path === '/api/orders/1050/active-call' || path === `/api/sessions/${activeId}`) return fulfill(calls[1]);
    if (path === `/api/sessions/${activeId}/connect`) {
      calls[1].state = 'CONNECTED';
      return fulfill(calls[1]);
    }
    if (path === `/api/sessions/${activeId}/context-confirmation`) {
      calls[1].state = 'DISCOVERY';
      return fulfill(calls[1]);
    }
    return fulfill({ detail: `Unexpected mock route ${path}` }, 404);
  });
  let connections = 0;
  let closed = 0;
  await page.routeWebSocket('**/ws/sessions/**/voice', socket => {
    connections++;
    socket.onClose(() => { closed++; });
    setTimeout(() => socket.send(JSON.stringify({ type: 'ready', state: 'DISCOVERY' })), 20);
  });
  await mockAudio(page);
  return { requests, connections: () => connections, closed: () => closed };
}

async function navigateQuery(page: Page, query: string) {
  await page.evaluate(value => {
    history.pushState({}, '', `/voice${value}`);
    dispatchEvent(new PopStateEvent('popstate'));
  }, query);
}

test('generic voice selects the active browser order instead of the resolved first seed', async ({ page }) => {
  const backend = await mockSelectionBackend(page);
  await page.goto('/voice');
  await expect(page.getByRole('combobox', { name: 'Delivery order' })).toHaveValue('1050');
  expect(backend.connections()).toBe(0);
  await page.getByRole('button', { name: 'Connect conversation' }).click();
  await expect(page.getByRole('button', { name: 'Disconnect', exact: true })).toBeVisible();
  expect(backend.connections()).toBe(1);
  expect(backend.requests).toContain(`POST /api/sessions/${activeId}/connect`);
  expect(backend.requests).toContain(`POST /api/sessions/${activeId}/context-confirmation`);
  expect(backend.requests.some(request => request.includes('/api/orders/1042/'))).toBe(false);
});

test('query navigation closes the old socket and preserves the resolved-order admission explanation', async ({ page }) => {
  const backend = await mockSelectionBackend(page);
  await page.goto('/voice?order_id=1050');
  await page.getByRole('button', { name: 'Connect conversation' }).click();
  await expect(page.getByRole('button', { name: 'Disconnect', exact: true })).toBeVisible();
  await navigateQuery(page, '?order_id=1042');
  await expect(page.getByRole('combobox', { name: 'Delivery order' })).toHaveValue('1042');
  await expect.poll(() => backend.closed()).toBe(1);
  await page.getByRole('button', { name: 'Connect conversation' }).click();
  await expect(page.getByRole('alert')).toContainText('A delivery plan is already committed for this order.');
  expect(backend.connections()).toBe(1);
  await navigateQuery(page, '?order_id=1050');
  await expect(page.getByRole('combobox', { name: 'Delivery order' })).toHaveValue('1050');
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect(backend.connections()).toBe(1);
});

for (const query of [`?session_id=${activeId}`, `?session_id=${activeId}&order_id=1042`]) {
  test(`session ownership determines the displayed delivery for ${query}`, async ({ page }) => {
    const backend = await mockSelectionBackend(page);
    await page.goto(`/voice${query}`);
    const selection = page.getByRole('combobox', { name: 'Delivery order' });
    await expect(selection).toHaveValue('1050');
    await expect(selection).toBeDisabled();
    expect(backend.connections()).toBe(0);
    await page.getByRole('button', { name: 'Connect conversation' }).click();
    await expect(page.getByRole('button', { name: 'Disconnect', exact: true })).toBeVisible();
    expect(backend.requests.some(request => request.includes('/api/orders/1042/'))).toBe(false);
  });
}
