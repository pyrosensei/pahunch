import type { Call } from '../api';

export type RecordedTurn = { speaker: string; structured_payload?: { opening?: boolean } };
export type RecordedEvent = { event_type: string; payload_json: string };
export function eventData(event: RecordedEvent): Record<string, unknown> {
  try { return JSON.parse(event.payload_json); } catch { return {}; }
}
const activityNames: Record<string, string> = {
  REALTIME_CONNECTED: 'Customer PC connected', VAD_LISTENING: 'Microphone ready for the customer',
  VAD_SPEECH_STARTED: 'Customer speech detected', VAD_UTTERANCE_ENDED: 'Processing the customer reply',
  VOICE_TURN: 'Agent reply recorded', OPENING_LINE_SPOKEN: 'Delivery introduction generated',
  REALTIME_PLAYBACK_DONE: 'Agent playback finished; ready for the customer', REALTIME_DISCONNECTED: 'Customer PC disconnected',
};
export function recordedActivity(events: RecordedEvent[]) {
  const latest = [...events].reverse().find(event => activityNames[event.event_type]);
  return latest ? activityNames[latest.event_type] : 'No customer audio connection yet';
}
export function deliveryProgress(call: Call, turns: RecordedTurn[], events: RecordedEvent[]) {
  if (call.state === 'COMMITTED') return { title: 'Delivery plan saved', detail: 'The customer confirmed the plan and the courier instructions are ready. This is not a delivered parcel.', step: 5 };
  if (call.state === 'CANCELLED') return { title: 'Order cancelled', detail: 'Cancelled at the customer’s request before dispatch. No delivery plan was committed.', step: 5 };
  if (call.state === 'CANCELLED') return { title: 'Order cancelled', detail: 'The backend accepted cancellation before shipment. This conversation is closed.', step: 5 };
  if (['DECLINED', 'NO_ANSWER', 'UNREACHABLE', 'ESCALATED', 'TOOL_FAILURE', 'TURN_BUDGET_EXHAUSTED'].includes(call.state)) return { title: 'Conversation ended', detail: 'This session is closed. Review its recorded outcome below; create a fresh demo order to try another conversation.', step: 5 };
  if (call.transport === 'twilio') return { title: call.state === 'CALLING' ? 'Phone attempt pending' : 'Phone conversation in progress', detail: 'Phone status comes from the configured transport. This is not a PC microphone session.', step: 1 };
  const names = events.map(event => event.event_type);
  const connected = names.lastIndexOf('REALTIME_CONNECTED') > names.lastIndexOf('REALTIME_DISCONNECTED');
  if (!connected) return { title: 'Ready to start the voice demo', detail: 'Open the customer voice tab, connect, and allow its microphone. The agent then introduces this delivery and asks whether the customer is available.', step: 1 };
  const opening = [...events].reverse().find(event => event.event_type === 'OPENING_LINE_SPOKEN');
  if (opening && eventData(opening).error) return { title: 'The introduction could not play', detail: 'The PC session connected, but opening audio failed. Check the participant tab and audio/provider errors before continuing.', step: 2 };
  const customerSpoke = turns.some(turn => turn.speaker === 'CUSTOMER');
  if (!customerSpoke) return { title: opening ? 'Waiting for customer availability' : 'Agent is introducing the delivery', detail: opening ? 'The agent has introduced the order. Reply in the customer voice tab with availability or a delivery preference.' : 'The agent is preparing the order-specific introduction. Its spoken text and opening timings will appear here.', step: 2 };
  if (call.customer_confirmed) return { title: 'Customer confirmed the current plan', detail: 'The PC participant saves the confirmed plan automatically. A committed courier card appears after the backend accepts that save.', step: 4 };
  if (call.state === 'PENDING_CONFIRMATION') return { title: 'Waiting for explicit customer confirmation', detail: 'The agent has proposed delivery instructions. The customer must hear the current plan and explicitly agree before it can be saved.', step: 4 };
  if (names.lastIndexOf('VAD_UTTERANCE_ENDED') > names.lastIndexOf('VOICE_TURN')) return { title: 'Agent is preparing a reply', detail: 'The customer finished speaking. Speech recognition, planning and voice generation are being processed.', step: 3 };
  return { title: 'Discussing the delivery preference', detail: 'Customer and agent turns are recorded below. The agent will propose instructions once the delivery preference is clear.', step: 3 };
}

export const readinessText: Record<string, string> = {
  ACTIVE_CALL: 'A conversation is already prepared for this order', CAPACITY: 'Another demo conversation is using the single available slot',
  RESOLVED: 'This order already has a resolved conversation', COOLDOWN: 'Waiting before another attempt is allowed',
  ATTEMPT_LIMIT: 'The attempt limit has been reached', NOT_OUT_FOR_DELIVERY: 'Mark the order out for delivery to prepare its conversation',
  WINDOW_NOT_DUE: 'The delivery window is not due yet', WINDOW_EXPIRED: 'The delivery window has ended',
  ORDER_CLOSED: 'The order is closed', OPTED_OUT: 'The customer opted out', MISSING_METADATA: 'Delivery details need review',
  NO_RISK_TRIGGER: 'This order does not require a proactive confirmation call', AWAITING_SWEEP: 'Checking whether the order is ready',
};
