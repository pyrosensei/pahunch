import { dateTime, label } from '../api';
import type { JsonObject } from '../voice/controller';

const text = (value: unknown) => typeof value === 'string' ? value : undefined;
const object = (value: unknown): JsonObject | null => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : null;
const quote = (value: unknown) => {
  const source = object(value);
  return typeof source?.quote === 'string' ? source.quote : undefined;
};

export function VoicePlanDetails({ proposal, card }: { proposal?: JsonObject | null; card?: JsonObject | null }) {
  const primary = card || object(proposal?.primary);
  const fallback = object(proposal?.fallback);
  if (!primary) return null;
  const recipientSource = quote(primary.recipient_source);
  const addressSource = quote(primary.recipient_address_source);
  return <div className="voice-plan-details readable-plan">
    <dl><dt>{card ? 'Committed action' : 'Proposed action'}</dt><dd>{label(text(primary.action))}</dd>
      <dt>Delivery window · IST</dt><dd>{dateTime(text(primary.window_start))}<br />{dateTime(text(primary.window_end))}</dd>
      <dt>Recipient</dt><dd>{text(primary.named_recipient) || (primary.action === 'HAND_TO_CUSTOMER' ? 'Customer' : 'Not specified')}</dd>
      {text(primary.recipient_address) && <><dt>Recipient address</dt><dd>{text(primary.recipient_address)}</dd></>}
      {typeof primary.call_on_arrival === 'boolean' && <><dt>Call on arrival</dt><dd>{primary.call_on_arrival ? 'Yes' : 'No'}</dd></>}
      {typeof primary.delay_minutes === 'number' && <><dt>Retry delay</dt><dd>{primary.delay_minutes} minutes</dd></>}
      {fallback && !card && <><dt>If the primary plan cannot happen</dt><dd>{label(text(fallback.action))}{text(fallback.named_recipient) ? ` · ${text(fallback.named_recipient)}` : ''}{text(fallback.recipient_address) ? ` · ${text(fallback.recipient_address)}` : ''}{typeof fallback.delay_minutes === 'number' ? ` · After ${fallback.delay_minutes} minutes` : ''}{typeof fallback.max_retries === 'number' ? ` · ${fallback.max_retries} retries` : ''}</dd></>}
    </dl>
    {(recipientSource || addressSource) && <details className="plan-source"><summary>Customer source</summary><dl>
      {recipientSource && <><dt>Recipient</dt><dd>{recipientSource}</dd></>}
      {addressSource && <><dt>Address</dt><dd>{addressSource}</dd></>}
    </dl></details>}
    {Array.isArray(card?.instructions) && <><h3>Courier instructions</h3><ul>{card.instructions.filter((item): item is string => typeof item === 'string').map((item, i) => <li key={i}>{item}</li>)}</ul></>}
  </div>;
}
