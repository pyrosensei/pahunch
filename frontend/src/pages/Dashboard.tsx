import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Activity, ArrowUpRight, AudioLines, CheckCircle2, ChevronRight, Clock3, FileCheck2, LockKeyhole, MessageSquare, RefreshCw, ShieldCheck, UserRound } from 'lucide-react';
import { api, dateTime, label, terminal } from '../api';
import type { Order, Call } from '../api';
import { OperatorReview } from './OperatorReview';
import PhoneAutomation from '../components/PhoneAutomation';
import type { OperatorSummary } from './operator-types';
import './OperatorDashboard.css';
import { deliveryProgress, readinessText, recordedActivity } from './delivery-progress';

type Turn = { id: number; speaker: string; text: string; created_at: string; structured_payload?: { opening?: boolean; planner?: { action?: string; response_language?: string; wording_source?: string; fallback_reason?: string } } };
type Audit = { id: number; event_type: string; payload_json: string; created_at: string };
type DialerStatus = { running: boolean; transport: string; last_error?: string | null; orders: { order_id: number; skip_reason?: string | null }[]; phone?: { enabled?: boolean; configured?: boolean; console_verified?: boolean; streaming_verified?: boolean; gnani_approved?: boolean; session_approval_present?: boolean } };
const KEY = 'pahunch.operator-token';
const readKey = () => { try { return sessionStorage.getItem(KEY) || ''; } catch { return ''; } };
const ms = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value)} ms` : 'Not available';
function payload(event: Audit): Record<string, unknown> { try { return JSON.parse(event.payload_json); } catch { return {}; } }
function lifecycle(call: Call) {
  if (call.transport !== 'twilio') return call.state === 'CALLING' ? 'PC demo · waiting to connect' : `PC voice · ${label(call.state)}`;
  if (call.state === 'CALLING') return `Phone request · ${call.dispatch_state ? label(call.dispatch_state) : 'Awaiting dispatch'}`;
  return `Phone call · ${label(call.state)}`;
}
export default function Dashboard() {
  const [params, setParams] = useSearchParams(); const preferred = Number(params.get('order_id')) || null; const requestedSession = params.get('session_id') || '';
  const [orders, setOrders] = useState<Order[]>([]); const [calls, setCalls] = useState<Call[]>([]); const [status, setStatus] = useState<DialerStatus | null>(null); const [selected, setSelected] = useState(requestedSession); const [filter, setFilter] = useState('all'); const [tab, setTab] = useState('conversation');
  const [turns, setTurns] = useState<Turn[]>([]); const [events, setEvents] = useState<Audit[]>([]); const [error, setError] = useState(''); const [detailError, setDetailError] = useState(''); const [loading, setLoading] = useState(true); const [updated, setUpdated] = useState(''); const [revision, setRevision] = useState(0);
  const [token, setToken] = useState(readKey); const [tokenDraft, setTokenDraft] = useState(''); const [accessOpen, setAccessOpen] = useState(false); const [operator, setOperator] = useState<OperatorSummary | null>(null); const [operatorError, setOperatorError] = useState('');
  const [futurePhoneOpen, setFuturePhoneOpen] = useState(false); const [focusOrder, setFocusOrder] = useState<number | null>(preferred);
  const current = useRef(selected); current.current = selected;
  const transcript = useRef<HTMLDivElement>(null); const followLatest = useRef(true);
  useEffect(() => { followLatest.current = true; }, [selected]);
  useEffect(() => { if (followLatest.current && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight; }, [turns.length, selected, tab]);
  useEffect(() => { if (requestedSession) setSelected(requestedSession); }, [requestedSession]);
  useEffect(() => {
    let stopped = false; let timer: ReturnType<typeof setTimeout>; const abort = new AbortController();
    async function refresh() {
      try {
        if (document.hidden) return;
        const [nextOrders, nextCalls, nextStatus] = await Promise.all([api<Order[]>('/api/orders', { signal: abort.signal }), api<Call[]>(`/api/sessions?limit=100${preferred ? `&order_id=${preferred}` : ''}`, { signal: abort.signal }), api<DialerStatus>('/api/dialer/status', { signal: abort.signal })]);
        if (stopped) return; setOrders(nextOrders); setCalls(nextCalls); setStatus(nextStatus);
        if (!current.current || !nextCalls.some(call => call.id === current.current)) setSelected(nextCalls.find(call => !preferred || call.order_id === preferred)?.id || '');
        setError(''); setUpdated(new Date().toLocaleTimeString());
      } catch (failure) { if (!stopped) setError(`Refresh failed: ${(failure as Error).message}. Displayed data may be stale.`); }
      finally { if (!stopped) { setLoading(false); timer = setTimeout(refresh, 2000); } }
    }
    void refresh(); return () => { stopped = true; abort.abort(); clearTimeout(timer); };
  }, [preferred, revision]);
  useEffect(() => { setTurns([]); setEvents([]); setDetailError(''); }, [selected]);
  useEffect(() => {
    if (!selected) return; let cursor = 0; let history: Turn[] = []; let stopped = false; let timer: ReturnType<typeof setTimeout>; const abort = new AbortController();
    async function read() {
      try {
        if (document.hidden) return;
        const readTurns = async () => {
          let batch: Turn[]; do {
            batch = await api<Turn[]>(`/api/sessions/${selected}/turns?limit=200&after_id=${cursor}`, { signal: abort.signal });
            if (stopped) return history;
            if (batch.length) { history = [...history, ...batch]; cursor = batch[batch.length - 1].id; }
          } while (batch.length === 200); return history;
        };
        const [nextTurns, nextEvents] = await Promise.all([readTurns(), api<Audit[]>(`/api/sessions/${selected}/events`, { signal: abort.signal })]);
        if (!stopped) { setTurns([...nextTurns]); setEvents(nextEvents); setDetailError(''); }
      } catch (failure) { if (!stopped) setDetailError(`Conversation refresh failed. Displayed details may be stale. ${(failure as Error).message}`); }
      finally { if (!stopped) timer = setTimeout(read, 2000); }
    }
    void read(); return () => { stopped = true; abort.abort(); clearTimeout(timer); };
  }, [selected, revision]);
  useEffect(() => { setOperator(null); setOperatorError(''); }, [selected, token]);
  useEffect(() => {
    if (!selected || !token) return; let stopped = false; let timer: ReturnType<typeof setTimeout>; const abort = new AbortController();
    async function read() {
      try {
        if (document.hidden) return;
        const value = await api<OperatorSummary>(`/api/sessions/${selected}/operator-summary`, { headers: { 'X-Operator-Token': token }, signal: abort.signal });
        if (!stopped) { setOperator(value); setOperatorError(''); }
      } catch (failure) { if (!stopped) setOperatorError(`Operator details unavailable: ${(failure as Error).message}. Refresh before making changes.`); }
      finally { if (!stopped) timer = setTimeout(read, 2000); }
    }
    void read(); return () => { stopped = true; abort.abort(); clearTimeout(timer); };
  }, [selected, token, revision]);
  const call = calls.find(item => item.id === selected); const order = orders.find(item => item.id === call?.order_id);
  const visible = calls.filter(item => (!preferred || item.order_id === preferred) && (filter === 'all' || (filter === 'active' ? !terminal.has(item.state) : terminal.has(item.state))));
  const metrics = events.filter(event => event.event_type === 'VOICE_TURN').map(payload).at(-1);
  const activeOperator = operator?.session_id === selected ? operator : null;
  const progress = call ? deliveryProgress(call, turns, events) : null;
  const openingEvent = [...events].reverse().find(event => event.event_type === 'OPENING_LINE_SPOKEN');
  const openingMetrics = openingEvent ? payload(openingEvent) : null;
  const contextOrder = order || orders.find(item => item.id === (focusOrder || preferred));
  const readyOrders = orders.filter(item => !calls.some(session => session.order_id === item.id) && !['CANCELLED', 'DELIVERED'].includes(item.status));
  const canLaunch = contextOrder && !call && ['PENDING', 'OUT_FOR_DELIVERY', 'RESCHEDULED'].includes(contextOrder.status);
  const openingRecorded = turns.some(turn => turn.speaker === 'AGENT' && turn.structured_payload?.opening);
  const firstReplyRecorded = turns.some(turn => turn.speaker === 'CUSTOMER');
  const customerRequests = activeOperator?.customer_requests || turns.filter(turn => turn.speaker === 'CUSTOMER').map(turn => ({ turn_id: turn.id, text: turn.text, created_at: turn.created_at }));
  const phonePending = status?.phone?.enabled ? [['configured', 'connection details'], ['console_verified', 'basic phone verification'], ['streaming_verified', 'streaming access'], ['gnani_approved', 'Gnani permission'], ['session_approval_present', 'session approval']].filter(([key]) => !status.phone?.[key as keyof NonNullable<DialerStatus['phone']>]).map(([, name]) => name) : [];
  function storeKey(event: FormEvent) {
    event.preventDefault(); const value = tokenDraft.trim(); if (!value) return;
    try { sessionStorage.setItem(KEY, value); } catch { /* Tab-memory access still works if storage is blocked. */ }
    setOperator(null); setToken(value); setTokenDraft(''); setAccessOpen(false);
  }
  function clearKey() { try { sessionStorage.removeItem(KEY); } catch { /* No persistent fallback. */ } setOperator(null); setToken(''); setTokenDraft(''); }
  const card = activeOperator?.courier_card || call?.courier_card;
  return <main id="main" className="dashboard-page operator-dashboard page">
    <header className="dispatch-header"><div><span className="eyebrow">PAHUNCH / DISPATCH DESK</span><h1>Delivery operations.</h1><p>The conversation, the customer’s request, the next delivery decision.</p></div><Link className="button primary" to="/shop">Create demo order<ArrowUpRight size={16}/></Link></header>
    <div className="dispatch-ledger" aria-label="Operations overview"><span className="dispatch-agent"><AudioLines size={17}/><strong>Pahunch Voice Agent</strong><span className="pill">{status?.transport === 'twilio' ? 'Phone transport' : 'PC demo'}</span></span><span><i className={`status-dot ${status?.running ? '' : 'neutral'}`}/>{status ? status.running ? 'Scheduler running' : 'Scheduler paused' : 'Connecting…'}</span><span><strong>{loading ? '—' : calls.filter(item => !terminal.has(item.state)).length}</strong> active</span><span><strong>{loading ? '—' : calls.filter(item => item.proposal && !item.customer_confirmed && !item.courier_card && !terminal.has(item.state)).length}</strong> awaiting confirmation</span><span><strong>{loading ? '—' : calls.filter(item => item.state === 'COMMITTED').length}</strong> plans saved</span><button className="text-link" onClick={() => setRevision(value => value + 1)}><RefreshCw size={13}/>{updated ? `Updated ${updated}` : 'Refresh'}</button></div>
    {error && <div className="notice error" role="alert">{error}</div>}{status?.last_error && <div className="notice error" role="status">Scheduler: {status.last_error}</div>}{!!phonePending.length && <div className="operator-readiness" role="status"><ShieldCheck size={17}/><span>Phone readiness blocked: {phonePending.join(', ')}.</span></div>}
    <section className="operator-access" aria-label="Operator access"><div><LockKeyhole size={14}/><strong>{token ? 'Operator access active' : 'Read-only monitoring'}</strong><small>{token ? 'Review actions are authorized by the backend.' : 'Unlock summaries and proposal review with your operator key.'}</small></div><button className="text-link" onClick={() => setAccessOpen(value => !value)}>{accessOpen ? 'Close access settings' : token ? 'Change operator key' : 'Enter operator key'}</button>{token && <button className="text-link" onClick={clearKey}>Clear access</button>}{accessOpen && <form onSubmit={storeKey}><label>Operator key<input aria-label="Operator key" type="password" value={tokenDraft} onChange={event => setTokenDraft(event.target.value)} autoComplete="off" spellCheck={false}/></label><button className="button primary small" disabled={!tokenDraft.trim()}>Use operator key</button><p>Stored only for this browser tab. Each change is checked and recorded by the backend.</p></form>}</section>
    <div className="workspace-grid operator-workspace">
      <aside className="panel session-panel"><div className="panel-heading"><h2>Agent activity</h2><span className="count-badge">{visible.length}</span></div><label className="sr-only" htmlFor="session-filter">Filter conversations</label><select id="session-filter" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">All sessions</option><option value="active">Active sessions</option><option value="completed">Completed sessions</option></select>{preferred && <button className="clear-filter" onClick={() => setParams({})}>Order #{preferred} × Clear filter</button>}
        <div className="session-list">{loading ? <p className="empty-copy">Loading conversations…</p> : visible.length ? visible.map(item => <button className={`session-card ${selected === item.id ? 'selected' : ''}`} key={item.id} aria-label={`Order #${item.order_id} · ${label(item.state)}`} onClick={() => { setSelected(item.id); setFocusOrder(item.order_id); }}><div><span className="session-avatar"><UserRound size={16}/></span><strong>#{item.order_id}</strong><ChevronRight size={14}/></div><span className={`pill ${item.state === 'COMMITTED' ? 'green' : ''}`}>{item.transport === 'browser' && item.state === 'CALLING' ? 'Ready for PC demo' : label(item.state)}</span><small>{dateTime(item.started_at)}</small></button>) : <p className="empty-copy">No conversations in this view.</p>}</div>
        {!!readyOrders.length && <section className="ready-deliveries"><h3>Orders without a conversation</h3>{readyOrders.filter(item => !preferred || item.id === preferred).map(item => <button className={`ready-order ${contextOrder?.id === item.id ? 'selected' : ''}`} key={item.id} onClick={() => { setSelected(''); setFocusOrder(item.id); setParams({ order_id: String(item.id) }); }}><strong>#{item.id}<ChevronRight size={13}/></strong><span>{label(item.status)}</span><small>{readinessText[status?.orders.find(skip => skip.order_id === item.id)?.skip_reason || 'AWAITING_SWEEP'] || 'Delivery details ready for review'}</small></button>)}</section>}
        <details className="order-queue"><summary>Delivery progress <span>{orders.length}</span></summary>{orders.map(item => <div className="queue-row" key={item.id}><strong>#{item.id} · {label(item.status)}</strong><small>{readinessText[status?.orders.find(skip => skip.order_id === item.id)?.skip_reason || 'AWAITING_SWEEP'] || 'Delivery is being reviewed'}</small></div>)}</details>
      </aside>
      <section className="panel conversation-panel operator-conversation"><div className="conversation-heading"><div><span className="eyebrow">{contextOrder ? `SWIFTCART / ORDER #${contextOrder.id}` : 'LIVE CONVERSATION'}</span><h2>{call ? 'The conversation' : contextOrder ? 'Delivery ready for review' : 'Select a delivery'}</h2></div>{call && !terminal.has(call.state) && call.transport === 'browser' && <Link className="button small secondary operator-pc-link" target="_blank" rel="noopener noreferrer" to={`/voice?session_id=${call.id}&order_id=${call.order_id}&pc_demo=1`}>Start customer voice demo<ArrowUpRight size={14}/></Link>}{canLaunch && <Link className="button small secondary operator-pc-link" target="_blank" rel="noopener noreferrer" to={`/voice?order_id=${contextOrder!.id}&pc_demo=1`}>Start PC demo now<ArrowUpRight size={14}/></Link>}</div>
        {contextOrder && <section className="delivery-purpose" aria-label="Reason for this conversation"><p>Confirm availability for <strong>{contextOrder.merchant_name} order #{contextOrder.id}</strong>.</p><dl><div><dt>Delivery window · IST</dt><dd>{dateTime(contextOrder.window_start)} – {dateTime(contextOrder.window_end)}</dd></div><div><dt>Payment & handoff</dt><dd>{contextOrder.payment_mode === 'COD' ? 'Cash on delivery' : 'Prepaid'} · {contextOrder.requires_otp ? 'OTP with courier at delivery' : 'No OTP is required'}</dd></div></dl><small>{contextOrder.delivery_address_summary}</small>{canLaunch && <p className="scheduled-demo-note">Start an explicit PC demo to rehearse this conversation before delivery; automatic dispatch still follows the delivery window.</p>}</section>}
        {progress && <section className="delivery-live-progress" aria-label="Delivery conversation progress"><span className={`status-dot ${terminal.has(call!.state) ? 'neutral' : ''}`}/><div><h3>{progress.title}</h3><p>{progress.detail}</p></div><span className="progress-counter">{progress.step + 1} / 6</span></section>}
        {call && <div className="operator-call-context"><span><AudioLines size={13}/>Pahunch <ChevronRight size={12}/> Customer #{call.order_id}</span><span>{lifecycle(call)} · {recordedActivity(events)}</span></div>}
        <div className="tabs" role="tablist" aria-label="Session details" onKeyDown={event => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) { event.preventDefault(); const next = event.key === 'Home' ? 'conversation' : event.key === 'End' ? 'performance' : tab === 'conversation' ? 'performance' : 'conversation'; setTab(next); document.getElementById(`tab-${next}`)?.focus(); } }}><button id="tab-conversation" role="tab" aria-label="Transcript" aria-controls="panel-conversation" tabIndex={tab === 'conversation' ? 0 : -1} aria-selected={tab === 'conversation'} onClick={() => setTab('conversation')}><MessageSquare size={15}/>Transcript<span>{turns.length}</span></button><button id="tab-performance" role="tab" aria-controls="panel-performance" tabIndex={tab === 'performance' ? 0 : -1} aria-selected={tab === 'performance'} onClick={() => setTab('performance')}><Activity size={15}/>Performance</button></div>
        {detailError && <p className="notice error" role="status">{detailError}</p>}
        {tab === 'conversation' ? <div className="transcript" ref={transcript} onScroll={event => { const node = event.currentTarget; followLatest.current = node.scrollHeight - node.scrollTop - node.clientHeight < 70; }} id="panel-conversation" role="tabpanel" aria-labelledby="tab-conversation">{turns.length ? turns.map(turn => <article className={`transcript-turn ${turn.speaker === 'AGENT' ? 'agent' : ''}`} key={turn.id}><div className="turn-label"><span>{turn.speaker === 'AGENT' ? 'PAHUNCH' : turn.speaker === 'CUSTOMER' ? 'CUSTOMER' : 'SYSTEM / OPERATOR'}</span><small>{dateTime(turn.created_at)}</small></div><p>{turn.text}</p>{turn.structured_payload?.planner && <details className="turn-meta"><summary>Response details</summary><span>{label(turn.structured_payload.planner.action)} · {turn.structured_payload.planner.response_language} · Wording: {turn.structured_payload.planner.wording_source}</span>{turn.structured_payload.planner.fallback_reason && <span>Reason: {label(turn.structured_payload.planner.fallback_reason)}</span>}</details>}</article>) : <div className="conversation-empty"><AudioLines size={28}/><h3>{call ? 'The agent speaks first.' : canLaunch ? 'Ready when you are.' : 'No conversation recorded.'}</h3><p>{call ? 'Open the customer voice demo and connect. The introduction and each reply will appear here.' : canLaunch ? 'Start the PC demo above, then connect and allow the microphone in the customer tab.' : 'Select an order from the left, or create a demo delivery in SwiftCart.'}</p><span>This desk monitors the call. The microphone lives in the customer tab.</span></div>}</div> : <div className="performance-panel" id="panel-performance" role="tabpanel" aria-labelledby="tab-performance"><h3>Latest reply timing</h3><p className="muted">Recorded server measurements. Device playback, speech endpoint detection and carrier delay are separate.</p>{metrics ? <><div className="metric-bars">{[['Speech recognition', metrics.stt_ms], ['Planning', metrics.planner_ms], ['Speech synthesis (full)', metrics.tts_ms]].map(([name, value]) => <div className="metric-bar" key={String(name)}><div><span>{String(name)}</span><strong>{ms(value)}</strong></div><div className="bar-track"><span style={{width:typeof value==='number' && Number.isFinite(value)?`${Math.max(0,value)/Math.max(Number(metrics.stt_ms)||0,Number(metrics.planner_ms)||0,Number(metrics.tts_ms)||0,1)*100}%`:'0%'}}/></div></div>)}</div><div className="timing-pair"><div><small>FIRST SUBSTANTIVE RESPONSE</small><strong>{ms(metrics.first_response_audio_ms)}</strong></div><div><small>TOTAL SERVER TURN</small><strong>{ms(metrics.total_ms)}</strong></div></div></> : <div className="performance-waiting"><Clock3 size={25}/><h3>{openingMetrics ? 'The introduction has its own timing.' : openingRecorded ? 'The introduction is being generated.' : 'Start the PC voice demo to measure it.'}</h3><p>{openingMetrics ? 'Opening audio does not use customer speech recognition or planning. The turn breakdown appears after the customer replies.' : openingRecorded ? 'The introduction is in the transcript. Its timing appears when audio generation finishes.' : 'No reply timing has been recorded. Connect the customer tab to hear the introduction, then reply to measure the full pipeline.'}</p>{openingMetrics && <div className="timing-pair"><div><small>INTRODUCTION · FIRST AUDIO</small><strong>{ms(openingMetrics.first_audio_ms)}</strong></div><div><small>INTRODUCTION · FULL SYNTHESIS</small><strong>{ms(openingMetrics.tts_ms ?? openingMetrics.total_ms)}</strong></div></div>}{openingMetrics?.error ? <p role="status">Opening audio failed. Review the customer tab’s audio error.</p> : null}</div>}</div>}
        <details className="audit-panel conversation-audit"><summary>Audit trail <span>{events.length}</span></summary>{events.map(event => <div className="audit-row" key={event.id}><strong>{label(event.event_type)}</strong><small>{dateTime(event.created_at)}</small><details><summary>Details</summary><pre>{JSON.stringify(payload(event), null, 2)}</pre></details></div>)}</details>
      </section>
      <aside className="outcome-column operator-outcomes">        {call ? <OperatorReview key={call.id} summary={activeOperator} fallbackPlan={call.proposal || null} confirmed={call.customer_confirmed} committed={!!call.courier_card} token={token} stale={!!operatorError || !!error} order={order} onUpdated={value => { if (current.current === value.session_id) { setOperator(value); setRevision(number => number + 1); } }}/> : <section className="panel"><h2>Delivery decision</h2><p className="operator-empty-copy">A validated proposal appears here after the customer discusses their delivery.</p></section>}
        <section className={`panel courier-panel ${card ? 'confirmed' : ''}`}><div className="panel-heading"><h2>Courier instructions</h2><FileCheck2 size={17}/></div>{card ? <><span className="pill green"><CheckCircle2 size={13}/>Plan committed</span><h3>{label(card.action)}</h3><ul className="courier-instructions">{card.instructions?.map((instruction: string, index: number) => <li key={index}>{instruction}</li>)}</ul><small>Confirmed instructions. The parcel is not marked delivered.</small></> : <p className="muted">Released after customer confirmation and commit.</p>}</section>
        <details className="delivery-context"><summary>Customer request & outcome</summary><section className="panel operator-understanding" aria-label="Customer needs"><div className="panel-heading"><h2>Customer request</h2><MessageSquare size={17}/></div>{customerRequests.length ? <><span className="request-count">{customerRequests.length} recorded customer {customerRequests.length === 1 ? 'turn' : 'turns'}</span>{customerRequests.slice(-3).map(request => <blockquote key={request.turn_id}><p>{request.text}</p><cite>{dateTime(request.created_at)}</cite></blockquote>)}{customerRequests.length > 3 && <small>Earlier requests remain in the full transcript.</small>}</> : <p className="operator-empty-copy">{firstReplyRecorded ? 'The customer’s recorded request is loading.' : 'The customer has not replied yet. Their request will appear after the first reply.'}</p>}</section>
        <section className="panel operator-understanding" aria-label="Conversation summary"><div className="panel-heading"><h2>Summary & outcome</h2><FileCheck2 size={17}/></div>{activeOperator ? <><span className="pill">{label(activeOperator.summary.status)}</span><p>{activeOperator.summary.text}</p><dl className="operator-facts"><dt>Recorded outcome</dt><dd>{activeOperator.outcome ? label(activeOperator.outcome) : 'No final outcome yet'}</dd></dl></> : <p className="operator-empty-copy">{token ? 'Waiting for verified operator details.' : 'Enter your operator key to view the recorded summary.'}</p>}{operatorError && <p className="notice error" role="status">{operatorError}</p>}</section>
</details>
        {!!activeOperator?.operator_events.length && <section className="panel operator-history"><h2>Operator decisions</h2>{activeOperator.operator_events.map(event => <article key={event.id}><strong>{label(event.action)}</strong><p>{event.reason}</p><small>{dateTime(event.created_at)}</small></article>)}</section>}
      </aside>
    </div>
    <details className="operator-future-phone" onToggle={event => setFuturePhoneOpen(event.currentTarget.open)}><summary>Future phone integration</summary>{futurePhoneOpen && <PhoneAutomation token={token}/>}</details>
    <p className="dispatch-footnote">Synthetic demo orders · counts cover the latest 100 sessions · a saved plan does not mean a completed delivery.</p>
  </main>;
}
