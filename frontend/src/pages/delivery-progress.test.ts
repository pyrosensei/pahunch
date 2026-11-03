import { describe, expect, it } from 'vitest';
import type { Call } from '../api';
import { deliveryProgress, readinessText, recordedActivity } from './delivery-progress';
const call = (state = 'CALLING'): Call => ({ id: 'one', order_id: 1050, state, transport: 'browser', customer_confirmed: false });
const event = (event_type: string, data = {}) => ({ event_type, payload_json: JSON.stringify(data) });
describe('recorded PC delivery progress', () => {
  it('explains that a prepared browser session still needs customer connection', () => {
    const result = deliveryProgress(call(), [], []);
    expect(result.title).toBe('Ready to start the voice demo');
    expect(result.detail).toContain('allow its microphone');
    expect(result.step).toBe(1);
  });
  it('distinguishes introduction generation from an introduction that finished', () => {
    const connected = event('REALTIME_CONNECTED');
    expect(deliveryProgress(call('DISCOVERY'), [], [connected]).title).toBe('Agent is introducing the delivery');
    expect(deliveryProgress(call('DISCOVERY'), [{ speaker: 'AGENT', structured_payload: { opening: true } }], [connected, event('OPENING_LINE_SPOKEN', { first_audio_ms: 650 })]).title).toBe('Waiting for customer availability');
  });
  it('does not claim an introduction was heard when opening synthesis failed', () => {
    expect(deliveryProgress(call('DISCOVERY'), [], [event('REALTIME_CONNECTED'), event('OPENING_LINE_SPOKEN', { error: 'TTS_UNAVAILABLE' })]).title).toBe('The introduction could not play');
  });
  it('uses a closed transport instead of stale opening events to request connection', () => {
    const result = deliveryProgress(call('DISCOVERY'), [], [event('REALTIME_CONNECTED'), event('OPENING_LINE_SPOKEN'), event('REALTIME_DISCONNECTED')]);
    expect(result.step).toBe(1);
  });
  it('keeps proposal, customer confirmation and saved instructions separate', () => {
    const turns = [{ speaker: 'CUSTOMER' }];
    const events = [event('REALTIME_CONNECTED')];
    expect(deliveryProgress(call('PENDING_CONFIRMATION'), turns, events).title).toContain('explicit customer confirmation');
    expect(deliveryProgress({ ...call('PENDING_CONFIRMATION'), customer_confirmed: true }, turns, events).title).toBe('Customer confirmed the current plan');
    expect(deliveryProgress(call('COMMITTED'), turns, events).title).toBe('Delivery plan saved');
    expect(deliveryProgress(call('COMMITTED'), turns, events).detail).toContain('not a delivered parcel');
  });
  it('translates scheduling reasons into explanations rather than showing queue codes', () => {
    expect(readinessText.CAPACITY).toContain('single available slot');
    expect(readinessText.RESOLVED).toContain('already');
  });
  it('shows only recorded audio activity, including disconnection rather than a fabricated live speaker', () => {
    expect(recordedActivity([])).toBe('No customer audio connection yet');
    expect(recordedActivity([event('VAD_LISTENING'), event('VAD_SPEECH_STARTED')])).toBe('Customer speech detected');
    expect(recordedActivity([event('VOICE_TURN'), event('REALTIME_DISCONNECTED')])).toBe('Customer PC disconnected');
  });
});
