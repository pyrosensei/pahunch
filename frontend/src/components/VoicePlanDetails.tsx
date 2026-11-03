import { dateTime, label } from '../api';
import type { JsonObject } from '../voice/controller';

const text = (value: unknown) => typeof value === 'string' ? value : undefined;
const object = (value: unknown): JsonObject | null => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : null;

export function VoicePlanDetails({ proposal, card }: { proposal?: JsonObject | null; card?: JsonObject | null }) {
  const primary = card || object(proposal?.primary);
  const fallback = object(proposal?.fallback);
  if (!primary) return null;
  return <div className="voice-plan-details readable-plan">
    <dl><dt>{card ? 'Committed action' : 'Proposed action'}</dt><dd>{label(text(primary.action))}</dd>
      <dt>Delivery window · IST</dt><dd>{dateTime(text(primary.window_start))}<br />{dateTime(text(primary.window_end))}</dd>
      <dt>Recipient</dt><dd>{text(primary.named_recipient) || (primary.action === 'HAND_TO_CUSTOMER' ? 'Customer' : 'Not specified')}</dd>
      {typeof primary.call_on_arrival === 'boolean' && <><dt>Call on arrival</dt><dd>{primary.call_on_arrival ? 'Yes' : 'No'}</dd></>}
      {typeof primary.delay_minutes === 'number' && <><dt>Retry delay</dt><dd>{primary.delay_minutes} minutes</dd></>}
      {fallback && !card && <><dt>If the primary plan cannot happen</dt><dd>{label(text(fallback.action))}{text(fallback.named_recipient) ? ` · ${text(fallback.named_recipient)}` : ''}{typeof fallback.delay_minutes === 'number' ? ` · After ${fallback.delay_minutes} minutes` : ''}{typeof fallback.max_retries === 'number' ? ` · ${fallback.max_retries} retries` : ''}</dd></>}
    </dl>
    {Array.isArray(card?.instructions) && <><h3>Courier instructions</h3><ul>{card.instructions.filter((item): item is string => typeof item === 'string').map((item, i) => <li key={i}>{item}</li>)}</ul></>}
  </div>;
}
