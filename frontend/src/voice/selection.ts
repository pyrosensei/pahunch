import type { VoiceOrder, VoiceSession } from './controller';

export interface DialerOrderSkip { order_id: number; skip_reason: string | null; }
const resumable = new Set(['CALLING', 'CONNECTED', 'DISCOVERY', 'NEGOTIATION', 'PENDING_CONFIRMATION']);

/** Selection is a suggestion only. Connection still goes through all server admission checks. */
export function defaultVoiceOrder(
  orders: VoiceOrder[], activeSessions: VoiceSession[], skipStates: DialerOrderSkip[],
): string {
  const browser = activeSessions.find(session => session.transport === 'browser' && resumable.has(session.state) && orders.some(order => order.id === session.order_id));
  if (browser) return String(browser.order_id);
  // The demo has one call slot. Do not suggest creating another call over an active phone/session.
  if (activeSessions.length) return '';
  const candidates = orders.filter(order => order.status === 'OUT_FOR_DELIVERY' && !skipStates.find(state => state.order_id === order.id)?.skip_reason);
  return String(candidates.sort((left, right) => right.id - left.id)[0]?.id || '');
}
