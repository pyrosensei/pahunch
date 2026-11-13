import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowLeft, Phone } from 'lucide-react';
import { api, terminal } from '../api';
import type { Call, Order } from '../api';
import { CustomerConversation } from '../components/CustomerConversation';

export default function CustomerCall() {
  const [params] = useSearchParams();
  const sessionId = params.get('session_id');
  const [delivery, setDelivery] = useState<{ call: Call; order: Order } | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    setDelivery(null); setError('');
    if (!sessionId) { setError('Open your delivery call from its incoming-call prompt or order page.'); return; }
    const abort = new AbortController();
    void (async () => {
      const call = await api<Call>(`/api/sessions/${encodeURIComponent(sessionId)}`, { signal: abort.signal });
      const order = await api<Order>(`/api/orders/${call.order_id}`, { signal: abort.signal });
      if (call.transport !== 'browser') throw new Error('This conversation uses a different calling channel. Open your order for its delivery status.');
      if (!terminal.has(call.state) && ['CANCELLED', 'DELIVERED'].includes(order.status)) throw new Error('This order is closed. Its delivery call is unavailable.');
      if (!abort.signal.aborted) setDelivery({ call, order });
    })().catch(failure => { if (!abort.signal.aborted) setError((failure as Error).message); });
    return () => abort.abort();
  }, [sessionId]);
  return <main id="main" className="customer-call-page page">
    <Link className="text-link" to={delivery ? `/shop?order_id=${delivery.order.id}` : '/shop'}><ArrowLeft size={17}/>Back to your order</Link>
    <header className="customer-call-title"><Phone size={23}/><div><span className="eyebrow">SWIFTCART DELIVERY ASSISTANT</span><h1>Your delivery call</h1>{delivery && <p>Order #{delivery.order.id} · Speak with Pahunch to arrange your delivery.</p>}</div></header>
    {error ? <p className="notice error" role="alert">{error}</p> : delivery ?
      <CustomerConversation key={delivery.call.id} sessionId={delivery.call.id} savedCard={delivery.call.courier_card} sessionState={delivery.call.state} autoConnect/> :
      <p role="status">Opening your delivery call…</p>}
  </main>;
}
