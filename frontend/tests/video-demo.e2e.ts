import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { catalog, mockAudio, order, planner, session } from './fixtures';

// These acceptance tests model the role boundary and commit lifecycle.
// Real provider and acoustic acceptance is recorded separately.
test('two-tab customer correction releases one current courier instruction card', async ({ context }, testInfo) => {
  const customer = await context.newPage();
  const consolePage = await context.newPage();
  await mockAudio(customer);
  await customer.addInitScript(() => {
    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable:true, value:async () => ({getTracks:() => [{stop() {}}]}) });
  });
  let currentOrder = {...order,payment_mode:'PREPAID',requires_otp:true};
  let current: Record<string, any> | null = null;
  let release: Record<string, any> | null = null;
  let commits = 0;
  let sockets = 0;
  let closed = 0;
  let generation = 0;
  let releaseFails = false;
  const writes: string[] = [];
  await context.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    const token = request.headers()['x-operator-token'];
    const fulfill = (json: unknown,status=200) => route.fulfill({json,status});
    if(method==='POST') writes.push(path);
    if(path==='/api/catalog') return fulfill(catalog);
    if(path==='/api/operator/access') return fulfill(token==='demo-key'?{authorized:true}:{detail:'A valid operator token is required'},token==='demo-key'?200:401);
    if(path==='/api/orders'&&method==='POST') return fulfill(currentOrder,201);
    if(path==='/api/orders') return fulfill([currentOrder]);
    if(path===`/api/orders/${order.id}`) return fulfill(currentOrder);
    if(path.endsWith('/out-for-delivery')) {
      if(token!=='demo-key') return fulfill({detail:'Operator access required'},401);
      currentOrder={...currentOrder,status:'OUT_FOR_DELIVERY'}; return fulfill(currentOrder);
    }
    if(path.endsWith('/call')) {
      if(token!=='demo-key'||!['PENDING','RESCHEDULED','OUT_FOR_DELIVERY'].includes(currentOrder.status)) return fulfill({detail:'Dispatch first'},409);
      current={...session,state:'CALLING'}; return fulfill(current,201);
    }
    if(path==='/api/sessions') return fulfill(current?[current]:[]);
    if(path===`/api/sessions/${session.id}`) return fulfill(current);
    if(path.endsWith('/active-call')) return current?fulfill(current):fulfill({detail:'No active call'},404);
    if(path.endsWith('/connect')||path.endsWith('/context-confirmation')) {
      current={...current,state:path.endsWith('/connect')?'CONNECTED':'DISCOVERY'}; return fulfill(current);
    }
    if(path.endsWith('/turns')||path.endsWith('/events')||path.endsWith('/slots')) return fulfill([]);
    if(path.endsWith('/operator-summary')) return fulfill({detail:'No proposal to review'},409);
    if(path.endsWith('/courier-instructions')) return releaseFails?fulfill({detail:'Temporary refresh failure'},503):fulfill(release);
    if(path.endsWith('/commit')) {
      commits++;
      expect(current?.customer_confirmed).toBe(true);
      const primary=current!.proposal.primary;
      const card={...primary,order_id:order.id,generated_at:session.started_at,instructions:[`Leave with neighbour: ${primary.named_recipient}, ${primary.recipient_address}`,'Courier must call the customer and verify OTP directly before handoff. If verification fails, do not hand over.']};
      release={order_id:order.id,session_id:session.id,commit_id:'commit-demo-one',committed_at:session.started_at,original_delivery_address:order.delivery_address_summary,contact:null,courier_card:card};
      current={...current,state:'COMMITTED',courier_card:card};
      return fulfill({state:'COMMITTED',commit_id:'commit-demo-one',courier_card:card});
    }
    return fulfill({detail:`Unexpected route ${path}`},404);
  });
  await customer.routeWebSocket('**/ws/sessions/**/voice', socket => {
    sockets++; socket.onClose(()=>{closed++;});
    socket.send(JSON.stringify({type:'ready',state:'DISCOVERY'}));
    socket.onMessage(raw=>{
      if(typeof raw!=='string')return;
      const event=JSON.parse(raw);
      if(event.type!=='scripted_turn')return;
      const corrected=event.text==='Sorry, not Aviral. Sharma ji instead.';
      const confirmed=event.text==='Yes';
      const primary={action:'LEAVE_WITH_NEIGHBOR',window_start:order.window_start,window_end:order.window_end,named_recipient:event.text==='Aviral Mishra at 403'?'Aviral Mishra':'Sharma ji',recipient_address:event.text==='Aviral Mishra at 403'?'403':'404',call_on_arrival:true};
      current={...current,state:corrected?'NEGOTIATION':'PENDING_CONFIRMATION',customer_confirmed:confirmed,proposal:corrected?null:confirmed?current?.proposal:{primary,fallback:null},verdict:corrected?null:{valid:true,violations:[]}};
      generation++;
      socket.send(JSON.stringify({type:'turn',generation_id:generation,state:current.state,transcript:event.text,agent_message:corrected?'Where should the courier find Sharma ji?':confirmed?'Your confirmation is recorded.':`Please confirm ${primary.named_recipient} at ${primary.recipient_address}.`,turn:{customer_confirmed:confirmed,verdict:current.verdict},planner:{...planner,missing_fields:corrected?['recipient_address']:[]}}));
      socket.send(JSON.stringify({type:'agent_audio_end',generation_id:generation}));
    });
  });
  await customer.goto('/shop');
  await customer.getByRole('button',{name:'Prepaid',exact:true}).click();
  await customer.getByRole('button',{name:'Place demo order'}).click();
  await expect(customer.getByRole('button',{name:'Connect conversation'})).toHaveCount(0);
  await expect(customer.getByRole('button',{name:'Mark out for delivery'})).toHaveCount(0);
  await expect(customer.getByRole('link',{name:'Dashboard',exact:true})).toHaveCount(0);
  await consolePage.goto('/dashboard');
  const dispatch=consolePage.getByRole('button',{name:'Mark out for delivery'});
  await expect(dispatch).toBeDisabled();
  await consolePage.getByRole('button',{name:'Enter operator key'}).click();
  await consolePage.getByLabel('Operator key',{exact:true}).fill('demo-key');
  await consolePage.getByRole('button',{name:'Use operator key'}).click();
  await consolePage.getByRole('button',{name:'Call customer'}).click();
  await expect(customer.getByRole('dialog', {name:'SwiftCart is calling.'})).toBeVisible();
  await customer.getByRole('button',{name:'Accept call'}).click();
  await expect(customer).toHaveURL(/\/call\?session_id=/);
  await expect(customer.getByRole('heading', {name:'Your delivery call',exact:true})).toBeVisible();
  await expect(customer.getByRole('button',{name:'Stop microphone'})).toBeVisible();
  await customer.getByText('Keyboard fallback',{exact:true}).click();
  async function send(text:string) {
    await customer.getByRole('textbox',{name:'Your reply'}).fill(text);
    await customer.getByRole('button',{name:'Send message'}).click();
    await expect(customer.getByRole('log')).toContainText(text);
  }
  await send('Aviral Mishra at 403');
  await send('Sorry, not Aviral. Sharma ji instead.');
  await expect(customer.getByRole('log')).toContainText('Where should the courier find Sharma ji?');
  expect(commits).toBe(0);
  await send('404');
  await send('Yes');
  await expect.poll(()=>commits).toBe(1);
  await consolePage.getByRole('tab',{name:'Courier instructions',exact:true}).click();
  const instructions=consolePage.getByRole('region',{name:'Released courier instructions'});
  await expect(instructions).toContainText('Sharma ji');
  await expect(instructions).toContainText('404');
  await expect(instructions).toContainText('verify OTP directly');
  await expect(instructions).toContainText(order.delivery_address_summary);
  await expect(instructions).not.toContainText('Aviral');
  await expect(instructions).not.toContainText('403');
  await expect(customer.getByRole('button',{name:'Disconnect conversation'})).toBeVisible();
  expect(closed).toBe(0);
  expect(sockets).toBe(1);
  expect(context.pages()).toHaveLength(2);
  await consolePage.screenshot({path:testInfo.outputPath('console-courier-release.png'),fullPage:true});
  await customer.screenshot({path:testInfo.outputPath('swiftcart-confirmed.png'),fullPage:true});
  releaseFails=true;
  await expect(consolePage.getByRole('status').filter({hasText:'Displayed information may be stale'})).toBeVisible();
  await expect(instructions).toContainText('Sharma ji');
  await customer.reload();
  await expect(customer.getByRole('button',{name:'Plan saved'})).toBeDisabled();
  await expect(customer.getByText('Your confirmed instructions are saved for the courier.')).toBeVisible();
  expect(commits).toBe(1);
  expect(writes.filter(path=>path.endsWith('/out-for-delivery'))).toHaveLength(0);
  expect(currentOrder.status).toBe('PENDING');
});

test('storefront and operator console expose accessible role-specific interfaces',async({page})=>{
  const {mockApi}=await import('./fixtures');
  await mockApi(page);
  for(const route of ['/shop','/dashboard']) {
    await page.goto(route);
    await expect(page.getByRole('heading',{level:1})).toBeVisible();
    const report=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();
    expect(report.violations).toEqual([]);
  }
});


test('a newly arrived order does not replace the selected delivery', async ({page})=>{
  const {mockApi}=await import('./fixtures');
  await mockApi(page);
  let rows=[order];
  await page.route('**/api/orders?**',route=>route.fulfill({json:rows}));
  await page.goto('/dashboard');
  await expect(page).toHaveURL(new RegExp(`order_id=${order.id}`));
  rows=[{...order,id:order.id+1},order];
  await page.getByRole('button',{name:/Updated/}).click();
  await expect(page.getByRole('button',{name:`Order #${order.id+1}`,exact:true})).toBeVisible();
  await expect(page.getByRole('button',{name:`Order #${order.id}`,exact:true})).toHaveAttribute('aria-pressed','true');
  await expect(page.getByRole('region',{name:'Selected delivery'})).toContainText(`ORDER #${order.id}`);
});
