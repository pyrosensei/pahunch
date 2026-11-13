import { useEffect, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowRight, Check, Package, ShieldCheck, Truck } from 'lucide-react';
import { api, dateTime, label, terminal } from '../api';
import type { Order, Call } from '../api';
import { CustomerConversation } from '../components/CustomerConversation';

type Product = { id: string; name: string; category: string; description: string; price_inr: number; art: string; direct_handoff_required: boolean };
const money = (value: number) => `₹${value.toLocaleString('en-IN')}`;
function ProductArt({ kind }: { kind: string }) {
  return <svg viewBox="0 0 240 210" aria-hidden="true" className={`catalog-illustration illustration-${kind}`}>
    <ellipse cx="120" cy="181" rx="70" ry="10" fill="currentColor" opacity=".09"/>
    {kind === 'headphones' ? <><path d="M59 119V96c0-81 122-81 122 0v23" fill="none" stroke="currentColor" strokeWidth="18"/><path d="M61 102V94c0-67 118-67 118 0v8" fill="none" stroke="#dce0e4" strokeWidth="5"/><rect x="44" y="100" width="41" height="65" rx="17" fill="currentColor" transform="rotate(-9 64 132)"/><rect x="155" y="100" width="41" height="65" rx="17" fill="currentColor" transform="rotate(9 176 132)"/><rect x="53" y="108" width="14" height="47" rx="7" fill="#ffffff" opacity=".17"/><rect x="173" y="109" width="14" height="47" rx="7" fill="#ffffff" opacity=".13"/></>
    : kind === 'backpack' ? <><path d="M78 68c-23 7-31 99-11 106m96-106c23 7 31 99 11 106" fill="none" stroke="#8c7654" strokeWidth="10"/><path d="M101 52v-9c0-18 38-18 38 0v9" fill="none" stroke="currentColor" strokeWidth="9"/><path d="M76 69c4-31 85-31 89 0l10 103c-3 17-107 17-110 0z" fill="currentColor"/><path d="M78 72c13-16 71-16 85 0" fill="none" stroke="#ffffff" strokeWidth="2" opacity=".35"/><rect x="83" y="118" width="74" height="49" rx="11" fill="#bd9c72"/><path d="M87 128h66" stroke="#72593b" strokeWidth="2"/><path d="M147 127v11" stroke="#e7d6be" strokeWidth="3"/><rect x="109" y="80" width="25" height="16" rx="2" fill="#eadac4"/></>
    : kind === 'watch' ? <><path d="M99 27h42l8 50H91zM91 134h58l-8 50H99z" fill="currentColor"/><path d="M111 33h18m-18 8h18m-18 8h18m-18 115h18m-18 8h18" stroke="#ffffff" strokeWidth="2" opacity=".2"/><rect x="78" y="64" width="85" height="86" rx="24" fill="#929aa5"/><rect x="84" y="70" width="73" height="74" rx="19" fill="#252c37"/><path d="M120 84v26l18 12" fill="none" stroke="#ffd447" strokeWidth="4" strokeLinecap="round"/><circle cx="120" cy="110" r="3" fill="#e5b66a"/><circle cx="120" cy="110" r="27" fill="none" stroke="#aab1bb" strokeWidth="1" opacity=".5"/><rect x="163" y="96" width="5" height="16" rx="2" fill="#69727e"/></>
    : <><path d="M60 40h129c5 0 8 4 7 9l-16 99H46z" fill="currentColor"/><path d="M66 47h122l-15 93H53z" fill="#e7edf4"/><path d="m54 136 49-58 30 40 16-24 29 44" fill="#b7c4d6"/><path d="m103 78 23 31-18-9-12 8z" fill="#ffffff"/><path d="M46 148h134l31 24c2 5-4 9-9 9H29c-5 0-9-4-5-9z" fill="#b0b6bf"/><path d="M55 154h117l19 14H37z" fill="#414a58"/><path d="M67 157h91m-99 4h107m-113 4h120" stroke="#c4cbd4" strokeWidth="1"/><path d="M92 171h49l6 6H87z" fill="#d9dde3"/></>}
  </svg>;
}

export default function Shop() {
  const [params, setParams] = useSearchParams();
  const requested = params.get('order_id');
  const [products, setProducts] = useState<Product[]>([]);
  const [productId, setProductId] = useState('');
  const [payment, setPayment] = useState('COD');
  const [order, setOrder] = useState<Order | null>(null);
  const [call, setCall] = useState<Call | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [trackingError, setTrackingError] = useState('');
  const [reload, setReload] = useState(0);
  const product = products.find(item => item.id === productId);
  useEffect(() => {
    const abort = new AbortController(); setLoading(true); setError('');
    void api<Product[]>('/api/catalog', { signal: abort.signal }).then(items => {
      setProducts(items); setProductId(current => current || items[0]?.id || '');
    }).catch(failure => { if (!abort.signal.aborted) setError((failure as Error).message); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [reload]);
  useEffect(() => {
    if (!requested || !/^\d+$/.test(requested)) { setOrder(null); setCall(null); return; }
    const abort = new AbortController();
    setOrder(null); setCall(null);
    void api<Order>(`/api/orders/${requested}`, { signal: abort.signal }).then(setOrder)
      .catch(failure => { if (!abort.signal.aborted) setError((failure as Error).message); });
    return () => abort.abort();
  }, [requested]);
  useEffect(() => {
    if (!order) return;
    const id = order.id;
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const [nextOrder, sessions] = await Promise.all([
          api<Order>(`/api/orders/${id}`, { signal: abort.signal }),
          api<Call[]>(`/api/sessions?order_id=${id}&limit=1`, { signal: abort.signal }),
        ]);
        if (abort.signal.aborted) return;
        setOrder(nextOrder); setCall(sessions[0] || null); setTrackingError('');
      } catch {
        if (!abort.signal.aborted) setTrackingError('Tracking could not refresh. Displayed delivery details may be out of date.');
      } finally { if (!abort.signal.aborted) timer = setTimeout(refresh, 2000); }
    }
    void refresh();
    return () => { abort.abort(); clearTimeout(timer); };
  }, [order?.id]);
  async function checkout() {
    if (busy || !product) return;
    setBusy(true); setError('');
    try {
      const created = await api<Order>('/api/orders', { method: 'POST', body: JSON.stringify({ payment_mode: payment, product_id: product.id }) });
      setParams({ order_id: String(created.id) });
    } catch (failure) { setError((failure as Error).message); }
    finally { setBusy(false); }
  }
  const prepared = call?.transport === 'browser' && !terminal.has(call.state) && !!order && ['PENDING', 'RESCHEDULED', 'OUT_FOR_DELIVERY'].includes(order.status);
  return <main id="main" className="shop-page page catalog-shop">
    {error && <div role="alert" className="notice error">{error}<button className="text-link" onClick={() => setReload(value => value + 1)}>Retry</button></div>}
    {!order && requested ? <section className="shop-loading"><h1>Your order</h1><p role="status">{error ? 'Order details are unavailable.' : 'Loading your delivery…'}</p></section> : order ? <>
      <div className="shop-order-title"><span className="eyebrow">YOUR SWIFTCART ORDER #{order.id}</span><h1>{call?.courier_card ? 'Your delivery plan is confirmed.' : order.status === 'PENDING' ? 'Let’s plan your delivery.' : order.status === 'RESCHEDULED' ? 'Your delivery has a new appointment.' : order.status === 'OUT_FOR_DELIVERY' ? 'Your parcel is on its way.' : label(order.status)}</h1><p>Follow your delivery and speak with your delivery assistant here.</p></div>
      <div className="customer-order-layout">
        <section className="customer-order-summary" aria-label="Your order details"><Package size={31}/><h2>Order #{order.id}</h2><span className="order-status">{label(order.status)}</span>
          <dl><dt>Delivery appointment</dt><dd>{dateTime(order.window_start)} – {dateTime(order.window_end)} IST</dd><dt>Delivery address</dt><dd>{order.delivery_address_summary}</dd><dt>Payment</dt><dd>{order.payment_mode === 'COD' ? 'Pay the courier on delivery' : 'Already paid'}</dd><dt>Verification at handoff</dt><dd>{order.requires_otp ? 'Share the OTP directly with the courier' : 'Standard handoff · no OTP'}</dd></dl>
          <ol className="customer-order-progress"><li className="complete"><Check size={15}/>Order placed</li><li className={['OUT_FOR_DELIVERY', 'DELIVERED'].includes(order.status) ? 'complete' : ''}><Truck size={15}/>Out for delivery</li><li className={call?.courier_card ? 'complete' : ''}><ShieldCheck size={15}/>Delivery instructions confirmed</li></ol>
          <button className="text-link" onClick={() => { setParams({}); setOrder(null); setCall(null); setError(''); }}>Back to the collection</button>
        </section>
        <div className="customer-order-main">
          {trackingError && <p className="notice error" role="status">{trackingError}</p>}
          {call?.transport === 'browser' && (prepared || terminal.has(call.state)) ? prepared ? <section className="customer-awaiting"><Truck size={38}/><h2>{call.state === 'CALLING' ? 'Your delivery assistant is calling.' : 'Your delivery call is ready.'}</h2><p>Answer the call to speak with Pahunch about this order.</p><Link className="button primary" to={`/call?session_id=${encodeURIComponent(call.id)}&order_id=${order.id}`}>{call.state === 'CALLING' ? 'Answer call' : 'Return to call'}</Link></section> : <CustomerConversation key={call.id} sessionId={call.id} savedCard={call.courier_card} sessionState={call.state}/> : call?.courier_card ? <section className="customer-delivery-saved"><ShieldCheck size={35}/><h2>Your delivery instructions are saved.</h2><p>The courier has your confirmed arrangement.</p><ul>{call.courier_card.instructions?.map((instruction: string) => <li key={instruction}>{instruction}</li>)}</ul></section> : <section className="customer-awaiting"><div className="tracking-parcel"><Package size={65} strokeWidth={1}/></div><span className="eyebrow">{order.status === 'PENDING' ? 'ORDER RECEIVED' : 'DELIVERY UPDATE'}</span><h2>{order.status === 'PENDING' ? 'We’re getting your order ready.' : order.status === 'OUT_FOR_DELIVERY' ? 'Your parcel has been dispatched.' : label(order.status)}</h2><p>{order.status === 'PENDING' ? 'Your delivery team will call to confirm when and where you can receive the parcel before dispatch.' : call && terminal.has(call.state) ? `The previous conversation ended with ${label(call.state).toLowerCase()}. The delivery team can review its recorded outcome.` : 'Your order status will update here. Keep this page open.'}</p></section>}
        </div>
      </div>
    </> : <>
      <div className="shop-hero"><div><span className="eyebrow">THE SWIFTCART EDIT</span><h1>Good things.<br/><em>Better arrivals.</em></h1><p>Everyday favourites, with a delivery conversation that fits your day.</p></div><div className="shop-hero-note"><Truck size={32} strokeWidth={1.3}/><strong>Your day has plans.<br/>Your delivery should too.</strong><span>Delivery coordination by Pahunch</span></div></div>
      <div className="checkout-layout"><section aria-label="Choose a product"><div className="section-label"><h2>The collection</h2><span>SELECT YOUR ITEM</span></div>
        {loading ? <p role="status">Opening the collection…</p> : <div className="product-grid">{products.map(item => <button key={item.id} className={`product-card ${productId === item.id ? 'selected' : ''}`} aria-label={`Choose ${item.name}`} aria-pressed={productId === item.id} onClick={() => setProductId(item.id)}><div className={`product-art catalog-art-${item.art}`}><span className="product-tag">{item.category}</span><ProductArt kind={item.art}/><span className="product-select">{productId === item.id && <Check size={16}/>}</span></div><div className="product-info"><h3>{item.name}</h3><p>{item.description}</p><strong>{money(item.price_inr)}</strong></div></button>)}</div>}
      </section>
      <aside className="checkout-panel"><span className="eyebrow">YOUR SELECTION</span><h2>Make it yours.</h2>{product && <div className="checkout-product"><ProductArt kind={product.art}/><div><strong>{product.name}</strong><span>{money(product.price_inr)}</span></div></div>}
        <form onSubmit={event => { event.preventDefault(); void checkout(); }}><fieldset><legend>Payment method</legend><div className="segmented"><button type="button" className={payment === 'COD' ? 'selected' : ''} aria-pressed={payment === 'COD'} onClick={() => setPayment('COD')}>Cash on delivery</button><button type="button" className={payment === 'PREPAID' ? 'selected' : ''} aria-pressed={payment === 'PREPAID'} onClick={() => setPayment('PREPAID')}>Prepaid</button></div></fieldset>
          <div className="verification-rule" role="status"><ShieldCheck size={21}/><div><strong>{payment === 'PREPAID' ? 'OTP required at delivery' : 'No OTP required'}</strong><p>{payment === 'PREPAID' ? 'Share the delivery OTP only with the courier at handoff. The assistant never collects it.' : 'Pay the courier at handoff. Cash-on-delivery orders require direct handoff to you.'}</p></div></div>
          {product?.direct_handoff_required && <p className="high-value-note">This laptop requires direct handoff to you. Neighbour and security drops are unavailable.</p>}
          <p className="checkout-promise">Your first appointment is tomorrow, 10 am–noon IST. Available alternatives run from 10 am to 6 pm.</p><div className="checkout-total"><span>Total</span><strong>{product ? money(product.price_inr) : '—'}</strong></div><button className="button primary full" disabled={busy || loading || !product}>{busy ? 'Placing your order…' : 'Place demo order'}<ArrowRight size={17}/></button><p className="checkout-note">Fictional order. No payment is collected.</p>
        </form>
      </aside></div>
    </>}
  </main>;
}
