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

test('Not now ends ringing and the operator can retry with a fresh invitation', async ({context}) => {
  const customer=await context.newPage();
  const operator=await context.newPage();
  await operator.clock.install();
  let current={...session,state:'CALLING',retry_after:'',remaining_attempts:2};
  let declines=0;
  let starts=0;
  await context.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    const fulfill=(json:unknown)=>route.fulfill({json});
    if(path==='/api/catalog')return fulfill([]);
    if(path==='/api/operator/access')return fulfill({authorized:true});
    if(path==='/api/orders')return fulfill([order]);
    if(path===`/api/orders/${order.id}`)return fulfill(order);
    if(path==='/api/sessions')return fulfill([current]);
    if(path.endsWith('/decline')){
      declines++;
      current={...current,state:'DECLINED',retry_after:new Date(Date.now()+30000).toISOString()};
      return fulfill(current);
    }
    if(path.endsWith('/call')){
      starts++;
      current={...current,id:'retry-invitation',state:'CALLING',remaining_attempts:1};
      return fulfill(current);
    }
    if(path.endsWith('/courier-instructions')||path.endsWith('/operator-summary'))return fulfill(null);
    if(path.endsWith('/events')||path.endsWith('/turns'))return fulfill([]);
    return route.fulfill({status:404,json:{detail:'Unexpected route'}});
  });
  await customer.goto(`/shop?order_id=${order.id}`);
  await operator.goto(`/dashboard?order_id=${order.id}`);
  await operator.getByRole('button',{name:'Enter operator key',exact:true}).click();
  await operator.getByRole('textbox',{name:'Operator key',exact:true}).fill('demo-key');
  await operator.getByRole('button',{name:'Use operator key',exact:true}).click();
  await expect(customer.getByRole('dialog')).toBeVisible();
  await customer.getByRole('button',{name:'Not now',exact:true}).click();
  await expect(customer.getByRole('dialog')).toHaveCount(0);
  await expect(operator.getByRole('heading',{name:'Customer asked to be called later.',exact:true})).toBeVisible();
  await expect(operator.getByText('Calling customer. Waiting for them to accept in SwiftCart.',{exact:true})).toHaveCount(0);
  const retry=operator.getByRole('button',{name:'Call again',exact:true});
  await expect(retry).toBeDisabled();
  await operator.clock.fastForward(31000);
  await expect(retry).toBeEnabled();
  await retry.click();
  await expect(customer.getByRole('dialog',{name:'SwiftCart is calling.'})).toBeVisible();
  expect(declines).toBe(1);
  expect(starts).toBe(1);
  expect(context.pages()).toHaveLength(2);
});

test('failed decline keeps the invitation visible and reports the failure',async({page})=>{
  await ringingCustomer(page);
  await page.route('**/api/sessions/*/decline',route=>route.fulfill({status:503,json:{detail:'Decline could not be saved. Try again.'}}));
  await page.goto(`/shop?order_id=${order.id}`);
  await page.getByRole('button',{name:'Not now',exact:true}).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('Decline could not be saved');
  await expect(page.getByRole('button',{name:'Accept call',exact:true})).toBeEnabled();
});

test('exhausted declined attempts keep Call again disabled',async({page})=>{
  await mockApi(page,{state:'DECLINED'});
  await page.route('**/api/sessions?**',route=>route.fulfill({json:[{...session,state:'DECLINED',remaining_attempts:0,retry_after:new Date(Date.now()-1000).toISOString()}]}));
  await page.goto('/dashboard');
  await expect(page.getByRole('button',{name:'Call again',exact:true})).toBeDisabled();
  await expect(page.getByRole('status')).toContainText('attempt limit');
});

test('closed orders cannot be retried even after an unanswered call',async({page})=>{
  await mockApi(page,{state:'DECLINED'});
  await page.route('**/api/orders?**',route=>route.fulfill({json:[{...order,status:'CANCELLED'}]}));
  await page.route('**/api/sessions?**',route=>route.fulfill({json:[{...session,state:'DECLINED',remaining_attempts:2,retry_after:new Date(Date.now()-1000).toISOString()}]}));
  await page.goto('/dashboard');
  await expect(page.getByRole('button',{name:'Call again',exact:true})).toBeDisabled();
  await expect(page.getByRole('status')).toContainText('This order is closed');
});
