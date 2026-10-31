(() => {
  const $ = id => document.getElementById(id);
  let order = null, busy = false;
  async function api(url, body) {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be completed.');
    return data;
  }
  function render() {
    $('summary').textContent = `Order #${order.id}\n${order.payment_mode} · ${order.status}\nWindow: ${new Date(order.window_start).toLocaleString()} – ${new Date(order.window_end).toLocaleString()}`;
    $('ready').disabled = order.status !== 'PENDING';
    $('dashboard').href = `/?order_id=${order.id}`;
  }
  async function checkout(event) {
    event.preventDefault();
    if (busy) return;
    busy = true; $('place').disabled = true; $('ready').disabled = true;
    try {
      order = await api('/api/orders', { payment_mode: $('payment').value, parcel_type: $('parcel').value, requires_otp: $('otp').checked });
      render(); $('message').textContent = 'Synthetic order created. Mark it out for delivery when ready.';
    } catch (error) { $('message').textContent = error.message; }
    finally { busy = false; $('place').disabled = false; }
  }
  async function markReady() {
    if (!order || busy) return;
    busy = true; $('ready').disabled = true;
    try {
      order = await api(`/api/orders/${order.id}/out-for-delivery`);
      render(); $('message').textContent = 'Order is ready. Follow its scheduler status on the dashboard.';
    } catch (error) { $('message').textContent = error.message; $('ready').disabled = false; }
    finally { busy = false; }
  }
  $('ready').disabled = true;
  $('checkout').addEventListener('submit', checkout);
  $('ready').addEventListener('click', markReady);
  window.demo = { checkout, markReady };
})();
