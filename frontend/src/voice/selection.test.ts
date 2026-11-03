import { describe, expect, it } from 'vitest';
import { defaultVoiceOrder } from './selection';
import type { VoiceOrder, VoiceSession } from './controller';

const order = (id: number, status = 'OUT_FOR_DELIVERY'): VoiceOrder => ({ id, status, merchant_name: 'SwiftCart', payment_mode: 'COD', requires_otp: true, window_start: '2026-11-02T12:00:00Z', window_end: '2026-11-02T14:00:00Z' });
const active = (orderId: number, transport = 'browser'): VoiceSession => ({ id: `session-${orderId}`, order_id: orderId, transport, state: 'DISCOVERY' });
describe('voice order selection', () => {
  it('selects the active new demo instead of the first ascending seed order', () => {
    expect(defaultVoiceOrder([order(1042), order(1050)], [active(1050)], [{ order_id: 1042, skip_reason: 'CAPACITY' }, { order_id: 1050, skip_reason: 'ACTIVE_CALL' }])).toBe('1050');
  });
  it('chooses the newest ready order only when no call is active', () => {
    expect(defaultVoiceOrder([order(1042), order(1050), order(1051, 'PENDING')], [], [])).toBe('1050');
  });
  it('does not suggest a new call over the active phone transport', () => {
    expect(defaultVoiceOrder([order(1042), order(1050)], [active(1050, 'twilio')], [])).toBe('');
  });
  it('does not automatically select closed, pending, expired or cooldown orders', () => {
    expect(defaultVoiceOrder([order(1042), order(1043, 'DELIVERED'), order(1050, 'PENDING'), order(1051)], [], [{ order_id: 1042, skip_reason: 'WINDOW_EXPIRED' }, { order_id: 1051, skip_reason: 'COOLDOWN' }])).toBe('');
  });
});
