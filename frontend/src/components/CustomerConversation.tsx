import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { AudioLines, Mic, MicOff, PhoneOff, Send, LoaderCircle } from 'lucide-react';
import { VoiceController } from '../voice/controller';
import type { JsonObject } from '../voice/controller';
import { conversationState } from '../voice/presentation';
import { terminal } from '../api';
import { VoicePlanDetails } from './VoicePlanDetails';

export function CustomerConversation({ sessionId, savedCard, sessionState, autoConnect = false }: { sessionId: string; savedCard?: JsonObject | null; sessionState?: string; autoConnect?: boolean }) {
  const [controller] = useState(() => new VoiceController());
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot);
  const [text, setText] = useState('');
  const tail = useRef<HTMLDivElement>(null);
  const microphoneRequested = useRef(false);
  useEffect(() => {
    controller.setAutoCommit(true);
    if (autoConnect && !terminal.has(sessionState || '') && !savedCard) void controller.connect({ sessionId });
    return () => controller.disconnect();
  }, [controller, sessionId, autoConnect, sessionState, savedCard]);
  useEffect(() => { tail.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }, [state.lines.length]);
  const presentation = conversationState(state);
  const writable = state.connected && ['DISCOVERY', 'NEGOTIATION', 'PENDING_CONFIRMATION'].includes(state.session?.state || '');
  const pending = state.processing || state.commitBusy;
  const ended = terminal.has(state.session?.state || sessionState || '');
  useEffect(() => {
    if (autoConnect && writable && !microphoneRequested.current) {
      microphoneRequested.current = true;
      void controller.toggleMicrophone();
    }
  }, [autoConnect, writable, controller]);
  function send() { if (controller.sendText(text)) setText(''); }
  return <section className="customer-conversation" aria-label="Delivery conversation">
    <div className="customer-call-heading"><div className="call-symbol"><AudioLines size={27}/></div><div><span className="eyebrow">PAHUNCH DELIVERY ASSISTANT</span><h2>{ended ? savedCard || state.session?.courier_card ? 'Your delivery instructions are saved.' : 'The conversation has ended.' : state.connected ? presentation.label : 'Let’s arrange your delivery.'}</h2></div></div>
    <p role="status">{ended && !state.speaking ? 'The delivery team can review the recorded outcome.' : state.connected ? state.hint : 'The assistant will explain your delivery and ask what works for you.'}</p>
    {state.error && <p className="notice error" role="alert">{state.error}</p>}
    {state.plannerError && <p className="notice error" role="status">The agent needs attention. Follow its latest message. <code>{state.plannerError}</code></p>}
    <div className="customer-call-controls">
      {state.connected ? <>
        <button className="button primary" disabled={!writable} onClick={() => void controller.toggleMicrophone()}>{state.microphone ? <MicOff size={18}/> : <Mic size={18}/>} {state.microphone ? 'Stop microphone' : 'Start microphone'}</button>
        <button className="button secondary" disabled={!writable} onPointerDown={event => { event.currentTarget.setPointerCapture(event.pointerId); void controller.beginPushToTalk(); }} onPointerUp={() => controller.endPushToTalk()} onPointerCancel={() => controller.endPushToTalk()} onLostPointerCapture={() => controller.endPushToTalk()} onKeyDown={event => { if (!event.repeat && [' ', 'Enter'].includes(event.key)) { event.preventDefault(); void controller.beginPushToTalk(); } }} onKeyUp={event => { if ([' ', 'Enter'].includes(event.key)) { event.preventDefault(); controller.endPushToTalk(); } }} onBlur={() => controller.endPushToTalk()}>Hold to talk</button>
        <button className="icon-button" aria-label="Disconnect conversation" onClick={() => controller.disconnect()}><PhoneOff size={18}/></button>
      </> : <button className="button primary" disabled={state.connecting || ended || !!savedCard} onClick={() => void controller.connect({ sessionId })}>{state.connecting ? 'Connecting…' : state.session?.courier_card || savedCard ? 'Plan saved' : ended ? 'Conversation ended' : 'Connect conversation'}</button>}
    </div>
    {state.connected && <label className="customer-listen"><input type="checkbox" checked={state.autoListen} onChange={event => controller.setAutoListen(event.target.checked)}/>Keep listening after each reply</label>}
    <div className="customer-thread" role="log" aria-live="polite" aria-label="Conversation transcript">
      {state.lines.map(line => <article key={line.id} className={`customer-message ${line.speaker}`}><strong>{line.speaker === 'agent' ? 'Pahunch' : 'You'}</strong><p>{line.text}</p></article>)}<div ref={tail}/>
    </div>
    <details className="customer-keyboard voice-keyboard-fallback"><summary>Keyboard fallback</summary><form onSubmit={event => { event.preventDefault(); send(); }}><input aria-label="Your reply" value={text} onChange={event => setText(event.target.value)} disabled={!writable || pending} placeholder="Type a reply for testing"/><button className="icon-button" aria-label="Send message" disabled={!writable || pending || !text.trim()}>{pending ? <LoaderCircle size={19}/> : <Send size={19}/>}</button></form></details>
    {state.session?.proposal && <details className="customer-plan" open={!!state.session.courier_card}><summary>{state.session.courier_card ? 'Your confirmed delivery instructions' : 'Review the proposed arrangement'}</summary><VoicePlanDetails proposal={state.session.proposal} card={state.session.courier_card}/></details>}
    {state.session?.courier_card && <p className="customer-saved" role="status">Your confirmed instructions are saved for the courier.</p>}
    {savedCard && !state.session?.courier_card && <section className="customer-plan"><h3>Your confirmed delivery instructions</h3><VoicePlanDetails card={savedCard}/><p role="status">Your confirmed instructions are saved for the courier.</p></section>}
  </section>;
}
