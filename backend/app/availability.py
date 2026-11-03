"""Server-owned delivery availability; no model-written dates or origin claims."""

import re
from datetime import timedelta

from sqlmodel import Session, select

from app.models import DeliverySlot, DialerEvent, Order, OrderStatus
from app.policy import IST, _utc


def availability_question(text: str) -> bool:
    # Factual requests only. A time-change instruction still goes to the planner.
    return bool(re.search(
        r"(?:which|what).{0,40}(?:available|availability|options)|which.{0,10}slots?|available.{0,20}(?:time|slot)|"
        r"कौन.{0,20}(?:टाइम|समय|स्लॉट)|(?:टाइम|समय|स्लॉट).{0,20}(?:उपलब्ध|अवेलेबल)|"
        r"(?:kaun|konsa|kaunsa).{0,20}(?:time|slot)", text, re.IGNORECASE))


def availability_reply(order: Order, slots: list[DeliverySlot], language: str, *, rejected=False) -> str:
    ordered = sorted(slots, key=lambda slot: _utc(slot.start_at))
    windows = "; ".join(
        f"{_utc(s.start_at).astimezone(IST):%d %B %H:%M}"
        f"–{_utc(s.end_at).astimezone(IST):%H:%M} IST" for s in ordered[:3])
    if not windows:
        return {"en-IN": "No alternative delivery availability is recorded. This needs the delivery team's review; no change has been saved.",
                "hi-IN": "डिलीवरी का कोई वैकल्पिक समय दर्ज नहीं है। डिलीवरी टीम की समीक्षा चाहिए; अभी कोई बदलाव नहीं हुआ है।",
                "hi-en": "Alternative delivery time recorded nahi hai. Delivery team ki review chahiye; koi change save nahi hua."}[language]
    prefix = {"en-IN": "That requested window is outside this order's delivery availability. ",
              "hi-IN": "माँगा गया समय इस ऑर्डर की उपलब्ध डिलीवरी अवधि में नहीं है। ",
              "hi-en": "Maanga gaya time is order ki delivery availability mein nahi hai. "}[language] if rejected else ""
    return prefix + {"en-IN": f"Available delivery periods are {windows}. Delivery cannot be scheduled before the first available date. Which date and time within these periods work for you?",
                     "hi-IN": f"उपलब्ध डिलीवरी अवधि हैं: {windows}। पहली उपलब्ध तारीख से पहले डिलीवरी नहीं हो सकती। इनमें कौन सी तारीख और समय आपके लिए ठीक रहेगा?",
                     "hi-en": f"Available delivery periods hain: {windows}. Pehli available date se pehle delivery nahi ho sakti. Inmein kaunsi date aur time theek rahega?"}[language]


def expand_demo_availability(db: Session) -> None:
    """Upgrade old synthetic two-hour options, retaining their earliest date.

    Never change a scheduled promise, resolved order, or real merchant record.
    This is synthetic availability, not a claim about a carrier's transit route.
    """
    ids = db.exec(select(DialerEvent.order_id).where(
        DialerEvent.event_type == "ORDER_CREATED", DialerEvent.reason == "DEMO_CHECKOUT")).all()
    for order_id in set(ids):
        order = db.get(Order, order_id)
        if order is None or order.status in {OrderStatus.CANCELLED, OrderStatus.DELIVERED, OrderStatus.RESCHEDULED}:
            continue
        slots = list(db.exec(select(DeliverySlot).where(DeliverySlot.order_id == order_id)).all())
        if not slots:
            continue
        first = min(_utc(s.start_at).astimezone(IST) for s in slots)
        # Keep the existing days; only widen their synthetic operational hours.
        for slot in slots:
            day = _utc(slot.start_at).astimezone(IST)
            slot.start_at = day.replace(hour=8, minute=0, second=0, microsecond=0)
            slot.end_at = day.replace(hour=18, minute=0, second=0, microsecond=0)
        if len(slots) == 1:
            day = first + timedelta(days=1)
            db.add(DeliverySlot(order_id=order_id, start_at=day.replace(hour=8, minute=0, second=0, microsecond=0),
                                end_at=day.replace(hour=18, minute=0, second=0, microsecond=0)))
    db.commit()
