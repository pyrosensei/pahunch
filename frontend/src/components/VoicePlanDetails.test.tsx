import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { VoicePlanDetails } from './VoicePlanDetails';

describe('saved courier instructions', () => {
  it('shows a direct-customer reschedule and its handoff restrictions', () => {
    const html = renderToStaticMarkup(<VoicePlanDetails card={{
      action: 'RESCHEDULE', recipient_kind: 'CUSTOMER',
      handoff_restrictions: ['Customer-only handoff. Do not leave with a neighbour or security.'],
      instructions: ['Courier must verify OTP directly with the customer.'],
    }} />);
    expect(html).toContain('<dt>Recipient</dt><dd>Customer</dd>');
    expect(html).toContain('Customer-only handoff');
    expect(html).toContain('Courier must verify OTP');
  });

  it('keeps legacy cards readable without inventing recipient permissions', () => {
    const html = renderToStaticMarkup(<VoicePlanDetails card={{ action: 'RESCHEDULE', instructions: [] }} />);
    expect(html).toContain('Not specified');
    expect(html).not.toContain('Handoff restrictions');
  });
});
