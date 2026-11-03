export type PlanStep = { action: string; window_start?: string | null; window_end?: string | null; call_on_arrival?: boolean; delay_minutes?: number | null; max_retries?: number; named_recipient?: string | null; notes?: string | null };
export type Proposal = { primary: PlanStep; fallback?: PlanStep | null; customer_statements?: string[]; notes?: string | null; confidence?: Record<string, number> };
export type Policy = { valid: boolean; violations: { code: string; message?: string }[]; allowed_alternatives: string[] };
export type OperatorSummary = {
  session_id: string; order_id: number; state: string; outcome: string | null; transport: string; dispatch_state: string | null; phone_consent: boolean;
  customer_requests: { turn_id: number; text: string; created_at: string }[];
  proposal: { id: string; plan: Proposal; source: 'customer' | 'operator'; policy: Policy | null; customer_confirmed: boolean } | null;
  courier_card: (PlanStep & { instructions?: string[] }) | null;
  operator_actions: { can_reject: boolean; can_revise: boolean; reason: string | null };
  operator_events: { id: number; action: string; reason: string; proposal_id: string | null; previous_proposal_id?: string | null; created_at: string }[];
  summary: { status: string; text: string };
  confirmation: { confirmed: boolean; requires_fresh_confirmation: boolean; presentation_pending?: boolean };
};
export const scheduled = new Set(['HAND_TO_CUSTOMER', 'LEAVE_WITH_SECURITY', 'LEAVE_WITH_NEIGHBOR', 'RESCHEDULE']);
