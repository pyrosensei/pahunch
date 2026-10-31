(() => {
  const $ = id => document.getElementById(id);
  const preferredOrder = Number(new URLSearchParams(location.search).get('order_id')) || null;
  let selected = null, busy = false, epoch = 0;
  async function api(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Dashboard request failed.');
    return data;
  }
  function card(parent, text, onClick) {
    const node = document.createElement(onClick ? 'button' : 'div');
    node.className = 'card'; node.textContent = text;
    if (onClick) node.addEventListener('click', onClick);
    parent.appendChild(node); return node;
  }
  async function selectCall(id) {
    selected = id; epoch++;
    $('transcript').replaceChildren(); $('details').textContent = 'Loading selected call…'; $('audit').textContent = '';
    if (!busy) await refresh();
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    const currentEpoch = epoch;
    try {
      const filter = $('filter').value;
      const suffix = filter === 'active' ? '&active=true' : filter === 'completed' ? '&active=false' : '';
      const [orders, calls, status] = await Promise.all([api('/api/orders'), api('/api/sessions?limit=100' + suffix), api('/api/dialer/status')]);
      if (currentEpoch !== epoch) return;
      $('orders').replaceChildren(); $('calls').replaceChildren();
      for (const order of orders) {
        const skip = status.orders.find(s => s.order_id === order.id);
        const row = card($('orders'), `#${order.id} · ${order.payment_mode} · ${order.status}\n${skip?.skip_reason || 'Awaiting sweep'}`);
        if (order.status === 'PENDING') {
          card(row, 'Mark out for delivery', async () => {
            try { await api(`/api/orders/${order.id}/out-for-delivery`, { method: 'POST' }); await refresh(); }
            catch (error) { $('message').textContent = error.message; }
          });
        }
      }
      if (!selected || !calls.some(c => c.id === selected)) selected = calls.find(c => !preferredOrder || c.order_id === preferredOrder)?.id || calls[0]?.id || null;
      for (const call of calls) {
        const node = card($('calls'), `#${call.order_id} · ${call.state}\n${call.transport} · ${call.trigger_reason}`, () => selectCall(call.id));
        if (call.id === selected) node.className += ' selected';
      }
      const call = calls.find(c => c.id === selected);
      if (call) {
        const sid = selected;
        const [turns, events, dialerEvents] = await Promise.all([api(`/api/sessions/${sid}/turns`), api(`/api/sessions/${sid}/events`), api(`/api/dialer/events?order_id=${call.order_id}&limit=50`)]);
        if (currentEpoch !== epoch || sid !== selected) return;
        $('conversation-title').textContent = `Conversation · #${call.order_id}`;
        $('transcript').replaceChildren();
        for (const turn of turns) {
          const meta = turn.structured_payload?.planner;
          card($('transcript'), `${turn.speaker}: ${turn.text}${meta ? `\n${meta.response_language} · ${meta.action} · wording ${meta.wording_source}${meta.fallback_reason ? ' · ' + meta.fallback_reason : ''}` : ''}`);
        }
        $('details').textContent = JSON.stringify({ state: call.state, outcome: call.outcome, customer_confirmed: call.customer_confirmed, transport: call.transport, dispatch_state: call.dispatch_state, phone_consent: call.phone_consent, proposal: call.proposal, verdict: call.verdict, courier_card: call.courier_card }, null, 2);
        $('audit').textContent = JSON.stringify({ session: events, dialer: dialerEvents }, null, 2);
      } else {
        $('transcript').replaceChildren(); $('details').textContent = 'No calls match this view.'; $('audit').textContent = '';
      }
      $('sync').textContent = `${status.running ? 'Scheduler running' : 'Scheduler stopped'} · ${status.transport} · refreshed ${new Date().toLocaleTimeString()}`;
      const phoneSteps = status.phone?.enabled ? [
        ['configured', 'phone connection details'], ['console_verified', 'basic phone test'],
        ['streaming_verified', 'streaming access'], ['gnani_approved', 'Gnani permission'],
        ['session_approval_present', 'approval for a phone test'],
      ].filter(([key]) => !status.phone[key]).map(([, label]) => label) : [];
      $('message').textContent = status.last_error ? `Scheduler error: ${status.last_error}`
        : phoneSteps.length ? `Phone setup pending: ${phoneSteps.join(', ')}. Browser fallback remains available.` : '';
    } catch (error) { $('message').textContent = `Refresh failed: ${error.message}. Displayed data may be stale.`; }
    finally { busy = false; }
  }
  $('refresh').addEventListener('click', refresh);
  $('filter').addEventListener('change', () => { epoch++; refresh(); });
  setInterval(refresh, 2000);
  window.demo = { refresh, selectCall, ready: refresh() };
})();
