import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowDown, ArrowUpRight, AudioLines, Check, CircleCheck, Headphones, Mic, MicOff, PhoneOff, Send, ShieldCheck, Sparkles } from 'lucide-react';
import { VoiceController } from '../voice/controller';
import type { JsonObject, VoiceOrder, VoiceSession } from '../voice/controller';
import { defaultVoiceOrder } from '../voice/selection';
import type { DialerOrderSkip } from '../voice/selection';
import { PlanProgress } from '../components/PlanProgress';
import { VoicePlanDetails } from '../components/VoicePlanDetails';
import { conversationState } from '../voice/presentation';
import './Voice.css';
import './VoiceStudio.css';

function readable(value: unknown): string {
  if (value == null) return '—';
  if (typeof value === 'object') return Object.entries(value as JsonObject).filter(([, item]) => item != null).map(([key, item]) => `${key.replaceAll('_', ' ')}: ${readable(item)}`).join(' · ');
  return String(value).replaceAll('_', ' ').toLowerCase();
}
export default function Voice() {
  const [controller] = useState(() => new VoiceController());
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const [params, setParams] = useSearchParams();
  const pcDemo = params.get('pc_demo') === '1';
  const sessionId = params.get('session_id') || undefined;
  const requestedOrder = params.get('order_id') || '';
  const [orders, setOrders] = useState<VoiceOrder[]>([]);
  const [orderId, setOrderId] = useState(requestedOrder);
  const [text, setText] = useState('');
  const [loadError, setLoadError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  useEffect(() => { controller.setAutoCommit(pcDemo); }, [controller, pcDemo]);
  useEffect(() => {
    const abort = new AbortController();
    controller.resetSelection(); setLoadError(null); setOrderId(requestedOrder);
    const read = async <T,>(path: string): Promise<T> => {
      const response = await fetch(path, { signal: abort.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Delivery details could not be loaded. Please refresh to try again.');
      return data as T;
    };
    void (async () => {
      const data = await read<VoiceOrder[]>('/api/orders');
      let selection: string;
      if (sessionId) {
        const session = await read<VoiceSession>(`/api/sessions/${encodeURIComponent(sessionId)}`);
        selection = String(session.order_id);
      } else if (requestedOrder) {
        if (!/^\d+$/.test(requestedOrder) || !data.some(order => order.id === Number(requestedOrder))) throw new Error('This delivery is unavailable. Choose another delivery or open the dashboard.');
        selection = requestedOrder;
      } else {
        const [sessions, status] = await Promise.all([
          read<VoiceSession[]>('/api/sessions?active=true&limit=100'),
          read<{ orders: DialerOrderSkip[] }>('/api/dialer/status'),
        ]);
        selection = defaultVoiceOrder(data, sessions, status.orders);
      }
      if (!abort.signal.aborted) { setOrders(data); setOrderId(selection); }
    })().catch(error => { if (!abort.signal.aborted) setLoadError(error instanceof Error ? error.message : 'Could not load orders.'); });
    return () => { abort.abort(); controller.disconnect(); };
  }, [controller, requestedOrder, sessionId]);
  useEffect(() => {
    if (!state.lines.length) return;
    const thread = bottom.current?.parentElement;
    thread?.scrollTo({ top: thread.scrollHeight, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  }, [state.lines.length]);
  const selected = orders.find(order => order.id === Number(orderId));
  const busy = state.connected || state.connecting;
  const presentation = conversationState(state);
  const writable = state.connected && ['DISCOVERY','NEGOTIATION','PENDING_CONFIRMATION'].includes(state.session?.state || '');
  const send = () => { if (controller.sendText(text)) setText(''); };
  const keyboardFallback = <form className="voice-compose" onSubmit={event => { event.preventDefault(); send(); }}><input aria-label="Your reply" placeholder="Prefer to type? Write your reply…" value={text} disabled={!writable} onChange={event => setText(event.target.value)} autoComplete="off" /><button aria-label="Send message" disabled={!writable || !text.trim()}><Send size={18} /></button></form>;
  return <main id="main" className="voice-page">
    <header className="voice-heading"><div><span className="voice-eyebrow"><span />{pcDemo ? 'CUSTOMER · PC VOICE' : 'THE CONVERSATION STUDIO'}</span><h1>A little conversation.<br /><em>A better delivery.</em></h1><p>{pcDemo ? 'Speak as the customer on this PC. Watch the agent and review its plan in the separate main monitor.' : 'Speak naturally. Pahunch turns your instructions into a delivery plan that follows the rules.'}</p></div><Link to={state.session ? `/dashboard?session_id=${state.session.id}&order_id=${state.session.order_id}` : orderId ? `/dashboard?order_id=${orderId}` : '/dashboard'} target={pcDemo ? '_blank' : undefined} rel={pcDemo ? 'noopener' : undefined} className="voice-dashboard-link">{pcDemo ? 'Open main monitor' : 'View your dashboard'} <ArrowUpRight size={17} /></Link></header>
    <div className="voice-layout">
      <aside className="voice-side">
        <section className="voice-order-card"><div className="voice-section-label"><span>01</span> YOUR DELIVERY</div><label htmlFor="voice-order">Choose a delivery</label><select id="voice-order" aria-label="Delivery order" disabled={busy || !!sessionId} value={orderId} onChange={event => { setOrderId(event.target.value); setParams({ ...(pcDemo ? { pc_demo: '1' } : {}), ...(event.target.value ? { order_id: event.target.value } : {}) }); }}><option value="">Select an order</option>{orders.map(order => <option key={order.id} value={order.id}>#{order.id} · {order.merchant_name}</option>)}</select>
          {sessionId && <p className="voice-session-note">This link selects a conversation. Availability is checked when you connect.</p>}
          {selected && <><div className="voice-parcel"><div className="voice-parcel-cube"><span /></div><div><strong>Order #{selected.id}</strong><span>{selected.merchant_name}</span></div></div><dl className="voice-order-facts"><div><dt>Payment</dt><dd>{selected.payment_mode === 'COD' ? 'Pay on delivery' : 'Already paid'}</dd></div><div><dt>Verification</dt><dd>{selected.requires_otp ? 'OTP at handoff' : 'Standard handoff'}</dd></div><div><dt>Delivery window</dt><dd>{new Date(selected.window_start).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' })} – {new Date(selected.window_end).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Kolkata' })} IST</dd></div></dl></>}
          {!state.connected ? <button className="voice-connect" disabled={state.connecting || (!orderId && !sessionId)} onClick={() => void controller.connect({ sessionId, orderId: Number(orderId) || undefined })}><Headphones size={17} />{state.connecting ? 'Connecting…' : 'Connect conversation'}</button> : <button className="voice-disconnect" onClick={() => controller.disconnect()}><PhoneOff size={16} /> Disconnect</button>}
          {loadError && <p className="voice-error" role="alert">{loadError}</p>}
          {!orderId && !sessionId && !loadError && <><p className="voice-session-note">No browser conversation is ready. Choose a delivery, or create a demo order and mark it out for delivery.</p><Link to="/shop" className="voice-small-link">Create a demo delivery <ArrowUpRight size={14} /></Link></>}
        </section>
        <section className="voice-assurance"><ShieldCheck size={22} /><h3>A helpful voice.<br />A careful decision.</h3><p>Delivery rules stay in control. Your plan is saved only after you clearly confirm it.</p><span><Check size={14} /> English · Hindi · Hinglish</span></section>
      </aside>
      <section className="voice-studio" aria-label="Conversation studio">
        <div className="voice-studio-header"><div className="voice-section-label"><span>02</span> LET’S TALK</div><span className={`voice-connection ${state.connected ? 'is-connected' : ''}`}><span />{state.connected ? state.session?.state.toLowerCase().replaceAll('_', ' ') : state.connecting ? 'Connecting' : 'Ready when you are'}</span></div>
        <div className={`conversation-state state-${presentation.key}`} data-voice-state={presentation.key}><div className="state-indicator" aria-hidden="true"><AudioLines size={26} strokeWidth={1.4}/></div><div><span className="voice-section-label">CONVERSATION STATE</span><h2>{presentation.label}</h2><p role="status" aria-live="polite">{state.hint}</p></div></div>
        {state.microphone && <div className="microphone-level"><span>Microphone input</span><meter aria-label="Microphone input level" min="0" max="1" value={state.level}/></div>}
        {state.error && <div className="voice-inline-error" role="alert">{state.error}</div>}
        <div className="voice-thread"><div className="voice-thread-title"><span>THE CONVERSATION</span><ArrowDown size={13} /></div><div className="voice-lines" role="log" aria-live="polite">{state.lines.length ? state.lines.map(line => <div key={line.id} className={`voice-line voice-line-${line.speaker}`}><span>{line.speaker === 'agent' ? 'Pahunch' : line.speaker === 'customer' ? 'You' : 'Update'}</span><p>{line.text}</p></div>) : <div className="voice-thread-empty"><Sparkles size={16} /><p>Your conversation will appear here.<br />Try “Please hand the parcel directly to me.”</p></div>}<div ref={bottom} /></div></div>
        <div className="voice-controls"><button className={`voice-mic ${state.microphone ? 'is-on' : ''}`} disabled={!writable} onClick={() => void controller.toggleMicrophone()}>{state.microphone ? <MicOff size={19} /> : <Mic size={19} />}{state.microphone ? 'Stop microphone' : 'Start microphone'}</button><button className="voice-ptt" disabled={!writable} onPointerDown={event => { event.currentTarget.setPointerCapture(event.pointerId); void controller.beginPushToTalk(); }} onPointerUp={() => controller.endPushToTalk()} onPointerCancel={() => controller.endPushToTalk()} onLostPointerCapture={() => controller.endPushToTalk()} onKeyDown={event => { if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) { event.preventDefault(); void controller.beginPushToTalk(); } }} onKeyUp={event => { if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); controller.endPushToTalk(); } }} onBlur={() => controller.endPushToTalk()}>Hold to talk</button></div>
        <label className="voice-auto"><input type="checkbox" checked={state.autoListen} onChange={event => controller.setAutoListen(event.target.checked)} /> Keep listening after each reply</label>
        {pcDemo ? <details className="voice-keyboard-fallback"><summary>Keyboard fallback</summary>{keyboardFallback}</details> : keyboardFallback}
        <div className="voice-bottom-note">Your microphone is only requested when you turn it on.</div>
      </section>
      <aside className="voice-plan-column"><section className="voice-plan-card"><div className="voice-section-label"><span>03</span> YOUR DELIVERY PLAN</div><div className="voice-plan-icon"><CircleCheck size={25} /></div><h2>{state.session?.courier_card ? 'All set for the courier.' : state.session?.customer_confirmed ? 'Confirmed by you.' : 'Clarity before action.'}</h2><p>{state.session?.courier_card ? 'Your confirmed instructions have been saved. The parcel has not been marked delivered.' : 'Tell us what works for you. We’ll check the rules, then ask you to confirm.'}</p>
          <PlanProgress proposed={!!state.session?.proposal} confirmed={!!state.session?.customer_confirmed} committed={!!state.session?.courier_card}/>
          {state.session?.verdict && <div className={`voice-verdict ${state.session.verdict.valid ? 'is-valid' : ''}`}><ShieldCheck size={15} />{state.session.verdict.valid ? 'Plan follows delivery policy' : 'This plan needs a different option'}{state.session.verdict.violations?.map(violation => <small key={violation.code}>{readable(violation.code)}</small>)}</div>}
          <VoicePlanDetails proposal={state.session?.proposal} card={state.session?.courier_card}/>
          {pcDemo ? <div className="voice-save" role="status"><Check size={16} />{state.commitBusy ? 'Saving your confirmed plan…' : state.session?.courier_card ? 'Plan saved' : 'Agent will save after your confirmation'}</div> : <button className="voice-save" disabled={!writable || !state.session?.customer_confirmed || state.commitBusy || !!state.session.courier_card} onClick={() => void controller.commit()}><Check size={16} />{state.commitBusy ? 'Saving…' : state.session?.courier_card ? 'Plan saved' : 'Save delivery plan'}</button>}<span className="voice-save-note">A clear confirmation comes first.</span>
        </section><details className="voice-metrics"><summary>Conversation diagnostics</summary><p className="diagnostics-note">Returned server timings; device playback and silence detection are excluded.</p>{[['First audio · may include acknowledgement','first_audio_ms'],['First substantive response','first_response_audio_ms'],['Speech recognition','stt_ms'],['Planner','planner_ms'],['Speech synthesis','tts_ms'],['Total server turn','total_ms']].map(([name,key])=><div key={key}><p>{name}</p><strong>{typeof state.timings[key]==='number' && Number.isFinite(state.timings[key]) ? `${Math.round(state.timings[key])} ms` : 'Not available'}</strong></div>)}{state.wording && <small>Wording and fallback: {state.wording}</small>}<Link to={state.session ? `/dashboard?session_id=${state.session.id}&order_id=${state.session.order_id}` : '/dashboard'}>Explore performance <ArrowUpRight size={13}/></Link></details></aside>
    </div>
  </main>;
}
