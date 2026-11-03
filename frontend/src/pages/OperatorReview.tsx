import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { PencilLine, ShieldCheck, X } from 'lucide-react';
import { api, dateTime, label } from '../api';
import type { Order } from '../api';
import { PlanProgress } from '../components/PlanProgress';
import type { OperatorSummary, PlanStep, Proposal } from './operator-types';
import { scheduled } from './operator-types';

type Slot = { id: number; start_at: string; end_at: string };
const actions = ['HAND_TO_CUSTOMER', 'LEAVE_WITH_SECURITY', 'LEAVE_WITH_NEIGHBOR', 'RESCHEDULE', 'RETRY_AFTER_DELAY', 'ESCALATE_TO_HUMAN'];
function PlanFacts({ plan }: { plan: Proposal | null }) {
  if (!plan) return <p className="operator-empty-copy">No delivery plan has been proposed.</p>;
  const step = plan.primary;
  return <dl className="operator-facts"><dt>Proposed action</dt><dd>{label(step.action)}</dd><dt>Recipient</dt><dd>{step.named_recipient || (step.action === 'HAND_TO_CUSTOMER' ? 'Customer' : 'Not specified')}</dd>
    {scheduled.has(step.action) && <><dt>Delivery window · IST</dt><dd>{dateTime(step.window_start || undefined)} – {dateTime(step.window_end || undefined)}</dd></>}
    <dt>Call on arrival</dt><dd>{step.call_on_arrival === false ? 'No' : 'Yes'}</dd>{step.delay_minutes != null && <><dt>Retry delay</dt><dd>{step.delay_minutes} minutes · {step.max_retries || 0} retries</dd></>}
    {plan.fallback && <><dt>If the primary plan cannot happen</dt><dd>{label(plan.fallback.action)}{plan.fallback.delay_minutes != null ? ` · After ${plan.fallback.delay_minutes} minutes` : ''}{plan.fallback.max_retries != null ? ` · ${plan.fallback.max_retries} retries` : ''}{plan.fallback.named_recipient ? ` · ${plan.fallback.named_recipient}` : ''}</dd></>}
  </dl>;
}
function StepFields({ title, step, change, slots, order }: { title: string; step: PlanStep; change: (value: PlanStep) => void; slots: Slot[]; order?: Order }) {
  const slotIndex = slots.findIndex(slot => new Date(slot.start_at).getTime() === new Date(step.window_start || '').getTime() && new Date(slot.end_at).getTime() === new Date(step.window_end || '').getTime());
  return <fieldset className="operator-step-fields"><legend>{title}</legend><label>Delivery action<select aria-label={`${title} action`} value={step.action} onChange={event => {
    const action = event.target.value;
    change({ ...step, action, window_start: scheduled.has(action) ? step.window_start || order?.window_start || null : null, window_end: scheduled.has(action) ? step.window_end || order?.window_end || null : null,
      delay_minutes: action === 'RETRY_AFTER_DELAY' ? step.delay_minutes || 15 : null, max_retries: action === 'RETRY_AFTER_DELAY' ? step.max_retries || 1 : 0,
      named_recipient: action === 'LEAVE_WITH_NEIGHBOR' || action === 'LEAVE_WITH_SECURITY' ? step.named_recipient || null : null });
  }}>{actions.map(action => <option key={action} value={action}>{label(action)}</option>)}</select></label>
    {scheduled.has(step.action) && <label>Delivery window · IST<select aria-label={`${title} delivery window`} value={slotIndex < 0 ? 'current' : String(slotIndex)} onChange={event => { const slot = slots[Number(event.target.value)]; if (slot) change({ ...step, window_start: slot.start_at, window_end: slot.end_at }); }}><option value="current">Current proposal: {dateTime(step.window_start || undefined)} – {dateTime(step.window_end || undefined)}</option>{slots.map((slot, index) => <option key={slot.id} value={index}>{dateTime(slot.start_at)} – {dateTime(slot.end_at)}</option>)}</select></label>}
    {(step.action === 'LEAVE_WITH_NEIGHBOR' || step.action === 'LEAVE_WITH_SECURITY') && <label>Recipient name<input aria-label={`${title} recipient name`} maxLength={80} value={step.named_recipient || ''} onChange={event => change({ ...step, named_recipient: event.target.value || null })}/></label>}
    {step.action === 'RETRY_AFTER_DELAY' && <div className="operator-field-pair"><label>Delay in minutes<input aria-label={`${title} delay minutes`} type="number" min={1} max={120} value={step.delay_minutes || 15} onChange={event => change({ ...step, delay_minutes: Number(event.target.value) })}/></label><label>Maximum retries<input aria-label={`${title} maximum retries`} type="number" min={1} max={2} value={step.max_retries || 1} onChange={event => change({ ...step, max_retries: Number(event.target.value) })}/></label></div>}
    <label className="operator-checkbox"><input type="checkbox" checked={step.call_on_arrival !== false} onChange={event => change({ ...step, call_on_arrival: event.target.checked })}/>Call the customer on arrival</label>
  </fieldset>;
}
export function OperatorReview({ summary, fallbackPlan, confirmed, committed, token, stale, order, onUpdated }: { summary: OperatorSummary | null; fallbackPlan: Proposal | null; confirmed: boolean; committed: boolean; token: string; stale: boolean; order?: Order; onUpdated: (value: OperatorSummary) => void }) {
  const [mode, setMode] = useState<'reject' | 'revise' | null>(null);
  const [reason, setReason] = useState(''); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [notice, setNotice] = useState('');
  const [expected, setExpected] = useState(''); const [primary, setPrimary] = useState<PlanStep>({ action: 'HAND_TO_CUSTOMER' });
  const [fallback, setFallback] = useState<PlanStep>({ action: 'RETRY_AFTER_DELAY', delay_minutes: 15, max_retries: 1, call_on_arrival: true }); const [useFallback, setUseFallback] = useState(false); const [slots, setSlots] = useState<Slot[]>([]);
  const mounted = useRef(true); useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { if (mode !== 'revise' || !order) return; const abort = new AbortController(); void api<Slot[]>(`/api/orders/${order.id}/slots`, { signal: abort.signal }).then(setSlots).catch(() => {}); return () => abort.abort(); }, [mode, order?.id]);
  const proposal = summary?.proposal; const plan = summary ? proposal?.plan || null : fallbackPlan; const isConfirmed = summary ? summary.confirmation.confirmed : confirmed; const isCommitted = !!summary?.courier_card || committed;
  const unavailable = !token ? 'Enter your operator key to review or change a proposal.' : stale ? 'Refresh the operator details before making changes.' : !summary ? 'Operator details are not available.' : summary.operator_actions.reason || '';
  const changed = !!mode && expected !== proposal?.id;
  const reviewAllowed = mode === 'reject' ? summary?.operator_actions.can_reject : summary?.operator_actions.can_revise;
  function open(action: 'reject' | 'revise') { if (!proposal) return; setMode(action); setReason(''); setError(''); setNotice(''); setExpected(proposal.id); setPrimary({ ...proposal.plan.primary }); setUseFallback(!!proposal.plan.fallback); setFallback(proposal.plan.fallback ? { ...proposal.plan.fallback } : { action: 'RETRY_AFTER_DELAY', delay_minutes: 15, max_retries: 1, call_on_arrival: true }); }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!summary || !mode || !token || !reviewAllowed || !reason.trim() || busy || changed || stale) return;
    setBusy(true); setError(''); const action = mode;
    const body = { expected_proposal_id: expected, reason: reason.trim(), ...(action === 'revise' ? { proposal: { ...summary.proposal!.plan, primary, fallback: useFallback ? fallback : null } } : {}) };
    try { const updated = await api<OperatorSummary>(`/api/sessions/${summary.session_id}/operator/${action}`, { method: 'POST', headers: { 'X-Operator-Token': token }, body: JSON.stringify(body) });
      if (mounted.current) { onUpdated(updated); setMode(null); setNotice(action === 'reject' ? 'Proposal rejected. Customer confirmation has been cleared.' : 'Revision proposed. A fresh customer confirmation is required.'); }
    } catch (failure) { if (mounted.current) setError((failure as Error).message); } finally { if (mounted.current) setBusy(false); }
  }
  return <section className="panel operator-plan"><div className="panel-heading"><h2>Delivery decision</h2><ShieldCheck size={19}/></div><PlanProgress proposed={!!plan} confirmed={isConfirmed} committed={isCommitted}/><div className="operator-decision-status">{isCommitted ? 'Committed instructions' : isConfirmed ? 'Customer confirmed · not yet committed' : plan ? 'Proposed · not customer confirmed' : 'Awaiting a proposal'}</div>
    {proposal && <p className="operator-source">Source: {proposal.source === 'operator' ? 'Operator revision' : 'Customer conversation'}</p>}
    <PlanFacts plan={plan}/>{proposal?.policy && <div className={`operator-policy ${proposal.policy.valid ? 'valid' : 'invalid'}`}><ShieldCheck size={15}/><span>{proposal.policy.valid ? 'Policy checked' : 'Policy blocked this plan'}</span>{proposal.policy.violations.map(violation => <p key={violation.code}>{violation.message || label(violation.code)}</p>)}</div>}
    {summary?.confirmation.requires_fresh_confirmation && <p className="operator-consent-note">This plan needs a new explicit customer confirmation.</p>}
    {summary?.confirmation.presentation_pending && <p className="operator-consent-note">The agent must present the revised plan to the customer before confirmation can count.</p>}
    <div className="operator-actions"><button type="button" disabled={!summary?.operator_actions.can_revise || !token || stale || busy} onClick={() => open('revise')}><PencilLine size={14}/>Revise proposal</button><button type="button" className="operator-reject" disabled={!summary?.operator_actions.can_reject || !token || stale || busy} onClick={() => open('reject')}><X size={15}/>Reject proposal</button></div>
    {unavailable && <p className="operator-action-note">{unavailable}</p>}{notice && <p className="operator-notice" role="status">{notice}</p>}
    {mode && <form className="operator-edit-form" onSubmit={event => void submit(event)}><div className="operator-editor-heading"><h3>{mode === 'reject' ? 'Reject the current proposal' : 'Revise the delivery proposal'}</h3><button type="button" aria-label="Close proposal review" disabled={busy} onClick={() => setMode(null)}><X size={16}/></button></div>
      <p>{mode === 'reject' ? 'This clears the proposal and confirmation. It does not end the call or mark the parcel delivered.' : 'The backend checks this revision against delivery policy. The customer must explicitly confirm the new plan before any commit.'}</p>
      {changed && <p className="notice error" role="alert">The proposal changed. Close this editor and review the latest plan.</p>}
      {mode === 'revise' && <><StepFields title="Primary plan" step={primary} change={setPrimary} slots={slots} order={order}/><label className="operator-checkbox"><input type="checkbox" checked={useFallback} onChange={event => setUseFallback(event.target.checked)}/>Include a fallback plan</label>{useFallback && <StepFields title="Fallback plan" step={fallback} change={setFallback} slots={slots} order={order}/>}</>}
      <label>Reason for this {mode === 'reject' ? 'rejection' : 'revision'}<textarea aria-label="Operator reason" required maxLength={500} value={reason} onChange={event => setReason(event.target.value)} placeholder="Explain the change for the audit trail."/></label>
      {error && <p className="notice error" role="alert">{error}</p>}
      <button className={`button ${mode === 'reject' ? 'operator-danger' : 'primary'}`} disabled={busy || !token || !reviewAllowed || !reason.trim() || changed || stale}>{busy ? 'Saving decision…' : mode === 'reject' ? 'Reject current proposal' : 'Submit revision for customer confirmation'}</button>
    </form>}
  </section>;
}
