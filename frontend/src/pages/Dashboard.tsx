import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { useSearchParams } from 'react-router-dom';
import { AudioLines, Phone, CheckCircle2, ChevronRight, FileCheck2, LockKeyhole, MessageSquare, RefreshCw, Truck } from 'lucide-react';
import { api, dateTime, label, terminal } from '../api';
import type { Order, Call } from '../api';
import { OperatorReview } from './OperatorReview';
import { VoicePlanDetails } from '../components/VoicePlanDetails';
import type { OperatorSummary, PlannerTurnMeta } from './operator-types';

type Turn = { id: number; speaker: string; text: string; created_at: string; structured_payload?: { opening?: boolean; planner?: PlannerTurnMeta } };
type Audit = { id: number; event_type: string; payload_json: string; created_at: string };
type CourierRelease = { order_id: number; session_id: string; commit_id: string; committed_at: string; original_delivery_address: string; contact: string | null; courier_card: Record<string, unknown> };
type WorkspaceTab = 'conversation' | 'review' | 'courier';
const KEY = 'pahunch.operator-token';
const readKey = () => { try { return sessionStorage.getItem(KEY) || ''; } catch { return ''; } };
const ms = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value)} ms` : 'Not available';
function payload(event: Audit): Record<string, unknown> { try { return JSON.parse(event.payload_json); } catch { return {}; } }
function TurnMeta({ planner }: { planner: PlannerTurnMeta }) {
  return <details className="turn-meta"><summary>Response details</summary>
    <span>{label(planner.action)} · {planner.response_language || 'Language not recorded'} · Wording: {label(planner.wording_source)}</span>
    {planner.fallback_reason && <span>Reason: {label(planner.fallback_reason)}</span>}
    {planner.error_code && <span className="turn-error">Error: <code>{planner.error_code}</code></span>}
    {planner.review_needed && <span className="review-needed" role="status">Review helpful · conversation can continue</span>}
    {!!planner.missing_fields?.length && <span>Missing: {planner.missing_fields.map(field => label(field)).join(', ')}</span>}
    {planner.language_warning && <span className="language-warning">Language warning: <code>{planner.language_warning}</code></span>}
  </details>;
}

export default function Dashboard() {
  const [params, setParams] = useSearchParams();
  const preferred = Number(params.get('order_id')) || null;
  const requestedSession = params.get('session_id');
  const [orders, setOrders] = useState<Order[]>([]);
  const [calls, setCalls] = useState<Call[]>([]);
  const [history, setHistory] = useState(false);
  const [tab, setTab] = useState<WorkspaceTab>('conversation');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [events, setEvents] = useState<Audit[]>([]);
  const [operator, setOperator] = useState<OperatorSummary | null>(null);
  const [courier, setCourier] = useState<CourierRelease | null>(null);
  const [courierLoadedAt, setCourierLoadedAt] = useState<string | null>(null);
  const [token, setToken] = useState(readKey);
  const [tokenDraft, setTokenDraft] = useState('');
  const tokenInput = useRef<HTMLInputElement>(null);
  const [authorized, setAuthorized] = useState(false);
  const [accessOpen, setAccessOpen] = useState(false);
  const [error, setError] = useState('');
  const [detailError, setDetailError] = useState('');
  const [operatorError, setOperatorError] = useState('');
  const [dispatchError, setDispatchError] = useState('');
  const [accessError, setAccessError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [updated, setUpdated] = useState<string | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    setAuthorized(false); setAccessError('');
    if (token) void api('/api/operator/access', { headers: { 'X-Operator-Token': token }, signal: abort.signal })
      .then(() => { if (!abort.signal.aborted) setAuthorized(true); })
      .catch(failure => { if (!abort.signal.aborted) setAccessError((failure as Error).message); });
    return () => abort.abort();
  }, [token]);
  useEffect(() => {
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const [nextOrders, nextCalls] = await Promise.all([
          api<Order[]>(`/api/orders${history ? '' : '?catalog_only=true&limit=20'}`, { signal: abort.signal }),
          api<Call[]>('/api/sessions?limit=100', { signal: abort.signal }),
        ]);
        if (abort.signal.aborted) return;
        const selectedId = preferred || nextCalls.find(item => item.id === requestedSession)?.order_id;
        if (selectedId && !nextOrders.some(item => item.id === selectedId)) {
          nextOrders.push(await api<Order>(`/api/orders/${selectedId}`, { signal: abort.signal }));
        }
        if (abort.signal.aborted) return;
        setOrders([...nextOrders].sort((a, b) => b.id - a.id)); setCalls(nextCalls); setError(''); setUpdated(new Date().toISOString());
      } catch (failure) { if (!abort.signal.aborted) setError(`Refresh failed. Displayed orders may be stale. ${(failure as Error).message}`); }
      finally { if (!abort.signal.aborted) { setLoading(false); timer = setTimeout(refresh, 2000); } }
    }
    void refresh(); return () => { abort.abort(); clearTimeout(timer); };
  }, [history, revision, preferred, requestedSession]);
  const requestedCall = requestedSession ? calls.find(item => item.id === requestedSession) : null;
  const orderId = requestedCall?.order_id || preferred || orders[0]?.id;
  const order = orders.find(item => item.id === orderId);
  const call = requestedCall || calls.find(item => item.order_id === orderId);
  const sessionId = call?.id;
  useEffect(() => {
    if (!preferred && !requestedSession && orders.length) {
      setParams({ order_id: String(orders[0].id) }, { replace: true });
    }
  }, [preferred, requestedSession, orders, setParams]);
  useEffect(() => {
    setTurns([]); setEvents([]); setOperator(null); setCourier(null); setCourierLoadedAt(null); setDetailError(''); setOperatorError(''); setDispatchError('');
  }, [orderId, sessionId]);
  useEffect(() => {
    if (!authorized) { setOperator(null); setCourier(null); }
  }, [authorized]);
  useEffect(() => {
    if (!orderId) return;
    const abort = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function read() {
      try {
        if (sessionId) {
          const [nextTurns, nextEvents] = await Promise.all([
            api<Turn[]>(`/api/sessions/${sessionId}/turns?limit=200`, { signal: abort.signal }),
            api<Audit[]>(`/api/sessions/${sessionId}/events`, { signal: abort.signal }),
          ]);
          if (abort.signal.aborted) return;
          setTurns(nextTurns); setEvents(nextEvents); setDetailError('');
        }
        if (authorized) {
          const headers = { 'X-Operator-Token': token };
          const [release, summary] = await Promise.allSettled([
            api<CourierRelease | null>(`/api/orders/${orderId}/courier-instructions`, { headers, signal: abort.signal }),
            sessionId ? api<OperatorSummary>(`/api/sessions/${sessionId}/operator-summary`, { headers, signal: abort.signal }) : Promise.resolve(null),
          ]);
          if (abort.signal.aborted) return;
          if (release.status === 'fulfilled') { setCourier(release.value); setCourierLoadedAt(new Date().toISOString()); }
          if (summary.status === 'fulfilled') setOperator(summary.value);
          const failure = release.status === 'rejected' ? release.reason : summary.status === 'rejected' ? summary.reason : null;
          setOperatorError(failure ? String(failure.message || failure) : '');
          if (failure) setDetailError(`Details could not refresh. Displayed information may be stale. ${failure.message || failure}`);
        }
      } catch (failure) {
        if (!abort.signal.aborted) {
          const message = `Details could not refresh. Displayed information may be stale. ${(failure as Error).message}`;
          setDetailError(message); setOperatorError(message);
        }
      } finally { if (!abort.signal.aborted) timer = setTimeout(read, 2000); }
    }
    void read(); return () => { abort.abort(); clearTimeout(timer); };
  }, [orderId, sessionId, authorized, token, revision]);
  useEffect(() => { if (accessOpen) tokenInput.current?.focus(); }, [accessOpen]);
  function storeKey(event: FormEvent) {
    event.preventDefault();
    const next = tokenDraft.trim(); if (!next) return;
    try { sessionStorage.setItem(KEY, next); } catch { /* Tab-memory access remains available. */ }
    setToken(next); setTokenDraft(''); setAccessOpen(false);
  }
  function clearKey() {
    try { sessionStorage.removeItem(KEY); } catch { /* No persistent fallback. */ }
    setToken(''); setAuthorized(false); setOperator(null); setCourier(null); setAccessOpen(true);
  }
  async function dispatch(action: 'ready' | 'prepare') {
    if (!order || !authorized || busy) return;
    setBusy(true); setDispatchError('');
    try {
      const path = action === 'ready' ? `/api/orders/${order.id}/out-for-delivery` : `/api/orders/${order.id}/call`;
      const options = { method: 'POST', headers: { 'X-Operator-Token': token } };
      if (action === 'ready') await api<Order>(path, options);
      else {
        const created = await api<Call>(path, options);
        setCalls(current => [created, ...current.filter(item => item.id !== created.id)]);
      }
      setRevision(value => value + 1);
    } catch (failure) { setDispatchError((failure as Error).message); }
    finally { setBusy(false); }
  }
  const metrics = events.filter(event => event.event_type === 'VOICE_TURN').map(payload).at(-1);
  const opening = events.filter(event => event.event_type === 'OPENING_LINE_SPOKEN').map(payload).at(-1);
  const tabs: [WorkspaceTab, string][] = [['conversation', 'Conversation'], ['review', 'Review'], ['courier', 'Courier instructions']];
  const canPrepare = !!order && ['PENDING', 'RESCHEDULED', 'OUT_FOR_DELIVERY'].includes(order.status) && !call;
  return <main id="main" className="operations-page">
    <header className="operations-title"><div><span className="eyebrow">DISPATCH DESK</span><h1>Every conversation.<br/><em>A clear next step.</em></h1></div><div className="operations-health"><span className="live-indicator"/>PC voice demo<button className="text-link" onClick={() => setRevision(value => value + 1)}><RefreshCw size={14}/>{updated ? `Updated ${new Date(updated).toLocaleTimeString('en-IN')}` : 'Refresh'}</button></div></header>
    <section className="operator-access" aria-label="Operator access"><div><LockKeyhole size={15}/><strong>{authorized ? 'Operator access active' : 'Read-only monitoring'}</strong><span>{authorized ? 'Dispatch and review actions are available.' : 'Enter your operator key to dispatch and review deliveries.'}</span></div><button className="text-link" onClick={() => setAccessOpen(value => !value)}>{accessOpen ? 'Close access settings' : 'Enter operator key'}</button>{token && <button className="text-link" onClick={clearKey}>Clear access</button>}{accessOpen && <form onSubmit={storeKey}><label>Operator key<input ref={tokenInput} type="password" aria-label="Operator key" value={tokenDraft} onChange={event => setTokenDraft(event.target.value)} autoComplete="off"/></label><button className="button primary" disabled={!tokenDraft.trim()}>Use operator key</button></form>}{accessError && <p className="notice error" role="alert">{accessError}</p>}</section>
    {error && <p className="notice error" role="alert">{error}</p>}
    <div className="operations-workspace"><aside className="operations-orders"><div className="operations-list-title"><h2>{history ? 'Order history' : 'Demo orders'}</h2><span>{orders.length}</span></div><label className="history-toggle"><input type="checkbox" checked={history} onChange={event => setHistory(event.target.checked)}/>Include older records</label><div className="operations-order-list">{loading ? <p role="status">Loading orders…</p> : !orders.length ? <p>No demo orders yet. Place an order in SwiftCart to begin.</p> : orders.map(item => {
      const session = calls.find(value => value.order_id === item.id);
      return <button key={item.id} className={`operations-order ${orderId === item.id ? 'selected' : ''}`} aria-label={`Order #${item.id}`} aria-pressed={orderId === item.id} onClick={() => setParams({ order_id: String(item.id) })}><span><strong>#{item.id}</strong><ChevronRight size={16}/></span><small>{item.merchant_name} · {item.payment_mode === 'COD' ? 'Cash on delivery' : 'Prepaid'}</small><span className={session?.courier_card ? 'order-result saved' : 'order-result'}>{session?.courier_card ? 'Instructions saved' : label(session?.state || item.status)}</span></button>;
    })}</div></aside>
    <section className="operations-detail" aria-label="Selected delivery">
      {order ? <><header className="selected-order-heading"><div><span className="eyebrow">SWIFTCART / ORDER #{order.id}</span><h2>{call?.courier_card ? 'Instructions confirmed.' : order.status === 'PENDING' ? 'Confirm availability before dispatch.' : call ? 'Conversation in progress.' : 'Ready for a conversation.'}</h2></div><span className="order-result">{label(order.status)}</span></header>
        <dl className="selected-order-facts"><div><dt>Delivery window · IST</dt><dd>{dateTime(order.window_start)} – {dateTime(order.window_end)}</dd></div><div><dt>Original delivery address</dt><dd>{order.delivery_address_summary}</dd></div><div><dt>Handoff</dt><dd>{order.payment_mode === 'COD' ? 'Cash on delivery · direct handoff' : order.requires_otp ? 'Prepaid · courier verifies OTP' : 'Prepaid'}</dd></div></dl>
        <div className="dispatch-actions">
          {!call && <button className="button primary" disabled={!authorized || !canPrepare || busy || !!error} onClick={() => void dispatch('prepare')}><Phone size={17}/>{busy ? 'Calling…' : 'Call customer'}</button>}
          {!authorized && <button className="button secondary" onClick={() => setAccessOpen(true)}><LockKeyhole size={17}/>Unlock calling</button>}
          {['PENDING', 'RESCHEDULED'].includes(order.status) && <button className="button secondary" disabled={!authorized || busy || !!error} onClick={() => void dispatch('ready')}><Truck size={17}/>{busy ? 'Updating…' : 'Mark out for delivery'}</button>}
          {call && !terminal.has(call.state) && <p role="status">{call.state === 'CALLING' ? 'Calling customer. Waiting for them to accept in SwiftCart.' : 'Customer call is active. Follow the conversation below.'}</p>}
          {order.status === 'PENDING' && <p>Call before the parcel leaves to confirm the date, time and recipient. The confirmed plan is saved for the courier; dispatch remains a separate action.</p>}
        </div>
        {dispatchError && <p className="notice error" role="alert">{dispatchError}</p>}
        <div className="workspace-tabs" role="tablist" aria-label="Delivery workspace" onKeyDown={event => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault(); const current = tabs.findIndex(([id]) => id === tab);
          const index = event.key === 'Home' ? 0 : event.key === 'End' ? 2 : (current + (event.key === 'ArrowRight' ? 1 : 2)) % 3;
          const next = tabs[index][0]; setTab(next); document.getElementById(`tab-${next}`)?.focus();
        }}>{tabs.map(([id, title]) => <button key={id} id={`tab-${id}`} role="tab" aria-selected={tab === id} aria-controls={`panel-${id}`} tabIndex={tab === id ? 0 : -1} onClick={() => setTab(id)}>{title}{id === 'courier' && courier && <CheckCircle2 size={14}/>}</button>)}</div>
        {detailError && <p className="notice error" role="status">{detailError}</p>}
        <div id={`panel-${tab}`} role="tabpanel" aria-labelledby={`tab-${tab}`}>
          {tab === 'conversation' && <><div className="operations-transcript" aria-label="Recorded conversation">{turns.length ? turns.map(turn => <article key={turn.id} className={`operations-turn ${turn.speaker.toLowerCase()}`}><header><strong>{turn.speaker === 'AGENT' ? 'Pahunch' : turn.speaker === 'CUSTOMER' ? 'Customer' : 'Operator / system'}</strong><time>{dateTime(turn.created_at)}</time></header><p>{turn.text}</p>{turn.structured_payload?.planner && <TurnMeta planner={turn.structured_payload.planner}/>}</article>) : <div className="operations-empty"><MessageSquare size={35}/><h3>{call ? 'Ready for the customer.' : 'The conversation will appear here.'}</h3><p>{call ? 'The customer accepts the incoming call in SwiftCart. Their talking page opens automatically and the assistant speaks first.' : 'Select Call customer to confirm availability before dispatch.'}</p></div>}</div>
          <details className="operations-diagnostics"><summary>Performance and audit details</summary><dl>{[['First substantive audio', metrics?.first_response_audio_ms], ['Speech recognition', metrics?.stt_ms], ['Planning', metrics?.planner_ms], ['Speech synthesis', metrics?.tts_ms], ['Total server turn', metrics?.total_ms], ['Introduction first audio', opening?.first_audio_ms]].map(([name, value]) => <div key={String(name)}><dt>{String(name)}</dt><dd>{ms(value)}</dd></div>)}</dl><p>Server timings exclude device playback and speech endpoint detection.</p><details><summary>Audit trail</summary>{events.map(event => <article key={event.id}><strong>{label(event.event_type)}</strong><small>{dateTime(event.created_at)}</small><pre>{JSON.stringify(payload(event), null, 2)}</pre></article>)}</details></details></>}
          {tab === 'review' && <div className="operations-review">{call ? <><OperatorReview key={call.id} summary={operator} fallbackPlan={call.proposal || null} confirmed={call.customer_confirmed} committed={!!call.courier_card} token={authorized ? token : ''} stale={!!operatorError || !!error} order={order} onUpdated={value => { setOperator(value); setRevision(number => number + 1); }}/><section className="operations-summary" aria-label="Conversation summary"><h3>Customer request & outcome</h3><p>{operator?.summary.text || 'Unlock operator access to read the verified summary.'}</p>{operator?.customer_requests.slice(-3).map(request => <blockquote key={request.turn_id}>{request.text}</blockquote>)}</section>{!!operator?.operator_events.length && <section className="operations-summary"><h3>Operator decisions</h3>{operator.operator_events.map(event => <article key={event.id}><strong>{label(event.action)}</strong><p>{event.reason}</p><small>{dateTime(event.created_at)}</small></article>)}</section>}</> : <div className="operations-empty"><FileCheck2 size={35}/><h3>Review follows the conversation.</h3><p>Validated proposals and customer confirmation will appear here.</p></div>}</div>}
          {tab === 'courier' && <section className="courier-release" aria-label="Released courier instructions">{courier ? <><div className="courier-release-heading"><CheckCircle2 size={29}/><div><span className="eyebrow">CUSTOMER CONFIRMED</span><h3>Ready for the courier.</h3></div></div><dl className="courier-reference"><dt>Order</dt><dd>#{courier.order_id}</dd><dt>Commit reference</dt><dd>{courier.commit_id}</dd><dt>Confirmed at</dt><dd>{dateTime(courier.committed_at)}</dd><dt>Original delivery address</dt><dd>{courier.original_delivery_address}</dd><dt>Customer contact</dt><dd>{courier.contact || 'Contact details are not recorded in this demo.'}</dd></dl><VoicePlanDetails proposal={call?.proposal || null} card={courier.courier_card}/><p className="courier-release-note">Loaded from the saved delivery commit. {courierLoadedAt && `Last retrieved ${dateTime(courierLoadedAt)}.`} The delivery outcome remains pending.</p></> : <div className="operations-empty"><FileCheck2 size={35}/><h3>{authorized ? 'Waiting for confirmed instructions.' : 'Operator access required.'}</h3><p>{authorized ? 'The courier’s instructions appear after explicit customer confirmation and a successful save.' : 'Unlock operator access to retrieve the saved courier instructions.'}</p></div>}</section>}
        </div>
      </> : <div className="operations-empty"><Truck size={35}/><h2>Select an order.</h2><p>New SwiftCart orders appear here automatically.</p></div>}
    </section></div>
  </main>;
}
