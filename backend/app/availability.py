"""Server-owned delivery availability; no model-written dates or origin claims."""

import re
from datetime import time, timedelta

from sqlmodel import Session, select

from app.models import DeliverySlot, DialerEvent, Order, OrderStatus
from app.policy import IST, _utc
from app.schemas import PlanStep


def availability_question(text: str) -> bool:
    # Factual requests only. A time-change instruction still goes to the planner.
    return bool(re.search(
        r"(?:which|what).{0,40}(?:available|availability|options)|which.{0,10}slots?|available.{0,20}(?:time|slot)|"
        r"कौन.{0,20}(?:टाइम|समय|स्लॉट)|(?:टाइम|समय|स्लॉट).{0,20}(?:उपलब्ध|अवेलेबल)|"
        r"(?:kaun|konsa|kaunsa).{0,20}(?:time|slot)", text, re.IGNORECASE))


def availability_reply(
    order: Order,
    slots: list[DeliverySlot],
    language: str,
    *,
    rejected=False,
    requested: PlanStep | None = None,
) -> str:
    after_hours = False
    requested_date = None
    if requested is not None and requested.window_start is not None and requested.window_end is not None:
        requested_start = _utc(requested.window_start).astimezone(IST)
        requested_end = _utc(requested.window_end).astimezone(IST)
        after_hours = (
            requested_start.date() != requested_end.date()
            or requested_start.time() >= time(18, 0)
            or requested_end.time() > time(18, 0)
        )
        requested_date = requested_start.date()

    alternatives = []
    for slot in slots:
        start = _utc(slot.start_at).astimezone(IST)
        end = _utc(slot.end_at).astimezone(IST)
        if after_hours and requested_date is not None and start.date() <= requested_date:
            continue
        start = max(start, start.replace(hour=10, minute=0, second=0, microsecond=0))
        end = min(end, end.replace(hour=18, minute=0, second=0, microsecond=0))
        if start < end:
            alternatives.append((start, end))
    alternatives.sort()
    windows = "; ".join(
        f"{start:%d %B %H:%M}-{end:%H:%M} IST" for start, end in alternatives[:3]
    )
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
            slot.start_at = day.replace(hour=10, minute=0, second=0, microsecond=0)
            slot.end_at = day.replace(hour=18, minute=0, second=0, microsecond=0)
        if len(slots) == 1:
            day = first + timedelta(days=1)
            db.add(DeliverySlot(order_id=order_id, start_at=day.replace(hour=10, minute=0, second=0, microsecond=0),
                                end_at=day.replace(hour=18, minute=0, second=0, microsecond=0)))
    db.commit()
