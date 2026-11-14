"""Explicit cancellation uses stored shipment state, never model assertions."""

import re

from sqlmodel import Session

from app.calls import ADMISSION_LOCK, CallService
from app.errors import DomainError
from app.models import CallOutcome, CallState, DialerEvent, OrderStatus, Speaker, Turn
from app.schemas import CustomerTurnRequest, PlannerAction, PlannerMeta, TurnResult
from app.state_machine import TERMINAL_STATES


def _intent(text: str, *, standalone: bool = False) -> str | None:
    text = text.strip().lower()
    text = re.sub(r"^(?:कोई नहीं|koi nahi)[,।\s]*", "", text)
    if not re.search(r"\b(?:cancel|cancellation|cancelled)\b|कैंसिल|कैंसल|कैनसिल|रद्द", text):
        return None
    # A mention, a negated instruction or a hypothetical is never permission.
    if re.search(r"don't|do not|not cancel|never cancel|मत|नहीं|nahi|mat\b|if |अगर|यदि", text):
        return None
    if re.search(r"^(?:why|when|how|what|who)\b|क्यों|किसने|कब रद्द|\b(?:said|says|told|quoted)\b|बोला|कहा था", text):
        return None
    if re.search(r"\b(?:can|could|may) (?:i|we)\b|cancellation policy|कैंसिल हो सकता|रद्द हो सकता|cancel kar sakta", text):
        return None if standalone else "question"
    match = re.search(r"\b(?:please )?cancel (?:my |the |this )?order\b|\bcancel it\b|\border cancellation please\b", text)
    if match and (not standalone or match.group() == text.rstrip(".!?। ")):
        return "request"
    match = re.search(r"(?:कैंसिल|कैंसल|कैनसिल|रद्द|\bcancel\b).{0,12}(?:कर|कीजिए)|cancel\s+kar(?:o|do| dijiye| de|na)", text)
    if match and (not standalone or match.group() == text.rstrip(".!?। ")):
        return "request"
    return None


def handle_cancellation(db: Session, session_id: str, text: str, language: str, *, require_standalone: bool = False) -> tuple[TurnResult, str, PlannerMeta] | None:
    intent = _intent(text, standalone=require_standalone)
    if intent is None:
        return None
    with ADMISSION_LOCK:
        db.expire_all()
        calls = CallService(db)
        call = calls.get_call(session_id)
        if call.state in TERMINAL_STATES:
            raise DomainError(409, "Call session is already terminal")
        order = calls._order(call.order_id)
        can_cancel = order.status == OrderStatus.PENDING
        english = language == "en-IN"
        if intent == "question" or not can_cancel:
            turn = calls.submit_turn(session_id, CustomerTurnRequest(text=text))
            if can_cancel:
                message = ("Your order has not shipped, so it can be cancelled. Would you like me to cancel it?" if english else
                           "आपका ऑर्डर अभी भेजा नहीं गया है, इसलिए इसे रद्द किया जा सकता है। क्या मैं इसे रद्द कर दूँ?")
            elif order.status == OrderStatus.DELIVERED:
                message = ("This order is already delivered, so I cannot cancel it. Please contact the merchant for a return." if english else
                           "यह ऑर्डर डिलीवर हो चुका है, इसलिए मैं इसे रद्द नहीं कर सकती। रिटर्न के लिए कृपया विक्रेता से संपर्क करें।")
            else:
                message = ("Your order has already been dispatched, so I cannot cancel it here. I can help arrange another available delivery time, or you can contact the merchant about cancellation." if english else
                           "आपका ऑर्डर डिलीवरी के लिए निकल चुका है, इसलिए मैं इसे यहाँ रद्द नहीं कर सकती। मैं उपलब्ध समय पर डिलीवरी बदलने में मदद कर सकती हूँ, या रद्द करने के लिए आप विक्रेता से संपर्क कर सकते हैं।")
            reason = "CANCELLATION_INFORMATION" if intent == "question" else "CANCELLATION_NOT_AVAILABLE"
        else:
            # Record request and disposition together. A cancellation is not a
            # delivery-plan confirmation and must never create a courier card.
            now = calls.clock()
            customer = Turn(session_id=call.id, speaker=Speaker.CUSTOMER, text=text,
                            structured_payload_json=CustomerTurnRequest(text=text).model_dump_json(), created_at=now)
            db.add(customer)
            db.flush()
            call.turn_count += 1
            call.customer_confirmed = False
            call.current_proposal_id = None
            calls._transition(call, CallState.CANCELLED)
            call.outcome = CallOutcome.CANCELLED
            call.ended_at = now
            order.status = OrderStatus.CANCELLED
            order.last_call_completed_at = now
            calls._audit(call.id, "ORDER_CANCELLED", {"order_id": order.id, "reason": "CUSTOMER_REQUEST"})
            calls._audit(call.id, "CALL_DISPOSITION", {"state": "CANCELLED", "reason": "ORDER_CANCELLED", "retryable_failure": False})
            db.add(DialerEvent(order_id=order.id, session_id=call.id, event_type="ORDER_CANCELLED", reason="CUSTOMER_REQUEST", created_at=now))
            db.commit()
            turn = TurnResult(session_id=call.id, turn_id=customer.id, state=call.state.value,
                              turn_count=call.turn_count, clarification_count=call.clarification_count,
                              customer_confirmed=False, outcome=call.outcome.value)
            message = ("Your order has been cancelled. It had not shipped, and no delivery will be arranged for this order." if english else
                       "आपका ऑर्डर रद्द कर दिया गया है। यह अभी भेजा नहीं गया था, और अब इस ऑर्डर की डिलीवरी नहीं की जाएगी।")
            reason = "ORDER_CANCELLED"
        return turn, message, PlannerMeta(model="rules:order-cancellation", action=PlannerAction.RESPOND,
                                         confidence=1.0, response_language=language, fallback_reason=reason)
