import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Phone, PhoneIncoming } from 'lucide-react';
import { api } from '../api';
import type { Call, Order } from '../api';

const CUSTOMER_ORDER = 'pahunch.customer-order';
function rememberedOrder() {
  try { return localStorage.getItem(CUSTOMER_ORDER) || ''; } catch { return ''; }
}

export function IncomingCall() {
  const location = useLocation();
  const navigate = useNavigate();
  const [orderId, setOrderId] = useState(rememberedOrder);
  const [offer, setOffer] = useState<{ call: Call; order: Order } | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const [declining, setDeclining] = useState(false);
  const [declineError, setDeclineError] = useState('');
  const customerPage = !['/dashboard', '/voice', '/call'].includes(location.pathname);
  const requestedOrder = new URLSearchParams(location.search).get('order_id');
  const targetOrder = location.pathname === '/shop' && requestedOrder && /^\d+$/.test(requestedOrder) ? requestedOrder : orderId;

  useEffect(() => {
    if (!customerPage) return;
    const requested = new URLSearchParams(location.search).get('order_id');
    if (location.pathname === '/shop' && requested && /^\d+$/.test(requested)) {
      setOrderId(requested);
      try { localStorage.setItem(CUSTOMER_ORDER, requested); } catch { /* The current order still works. */ }
    } else setOrderId(rememberedOrder());
    const changed = (event: StorageEvent) => { if (event.key === CUSTOMER_ORDER) setOrderId(rememberedOrder()); };
    window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, [customerPage, location.pathname, location.search]);

  useEffect(() => {
    setOffer(null);
    if (!customerPage || !/^\d+$/.test(targetOrder)) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const order = await api<Order>(`/api/orders/${targetOrder}`, { signal: abort.signal });
        const calls = ['PENDING', 'RESCHEDULED', 'OUT_FOR_DELIVERY'].includes(order.status)
          ? await api<Call[]>(`/api/sessions?order_id=${targetOrder}&active=true&limit=1`, { signal: abort.signal }) : [];
        if (abort.signal.aborted) return;
        const call = calls.find(item => item.order_id === order.id && item.transport === 'browser' && item.state === 'CALLING');
        setOffer(call ? { call, order } : null);
      } catch { if (!abort.signal.aborted) setOffer(null); }
      finally { if (!abort.signal.aborted) timer = setTimeout(refresh, 2000); }
    }
    void refresh();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [customerPage, targetOrder]);

  const visible = customerPage && offer && String(offer.order.id) === targetOrder && offer.call.id !== dismissed;
  useEffect(() => {
    const node = dialog.current;
    if (visible && node && !node.open) node.showModal();
    return () => { if (node?.open) node.close(); };
  }, [visible, offer?.call.id]);

  async function decline() {
    if (!offer || declining) return;
    setDeclining(true); setDeclineError('');
    try {
      await api(`/api/sessions/${encodeURIComponent(offer.call.id)}/decline`, {method:'POST'});
      setDismissed(offer.call.id); setOffer(null);
    } catch (failure) { setDeclineError((failure as Error).message); }
    finally { setDeclining(false); }
  }
  if (!visible) return null;
  return <dialog ref={dialog} className="incoming-call" aria-labelledby="incoming-call-title" aria-describedby="incoming-call-detail"
    onCancel={event => { event.preventDefault(); void decline(); }}>
    <div className="incoming-call-icon"><PhoneIncoming size={34}/></div>
    <p className="eyebrow">INCOMING DELIVERY CALL</p>
    <h2 id="incoming-call-title">SwiftCart is calling.</h2>
    <p id="incoming-call-detail">Pahunch delivery assistant · Order #{offer.order.id}</p>
    <p>Answer to arrange your delivery with the voice assistant.</p>
    {declineError && <p className="notice error" role="alert">{declineError}</p>}
    <div className="incoming-call-actions">
      <button className="button primary" autoFocus disabled={declining} onClick={() => {
        setDismissed(offer.call.id);
        navigate(`/call?session_id=${encodeURIComponent(offer.call.id)}&order_id=${offer.order.id}`);
      }}><Phone size={19}/>Accept call</button>
      <button className="button secondary" disabled={declining} onClick={() => void decline()}>{declining ? 'Declining…' : 'Not now'}</button>
    </div>
  </dialog>;
}
