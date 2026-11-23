"""Server-owned delivery availability; no model-written dates or origin claims."""

from datetime import datetime, time, timedelta

from sqlmodel import Session, select

from app.models import DeliverySlot, DialerEvent, Order, OrderStatus
from app.policy import IST, _utc
from app.schemas import PlanStep
from app.speech_text import customer_date, customer_window


def _covers_date(window: tuple[datetime, datetime], asked: tuple[int, int]) -> bool:
    day = window[0].date()
    last = window[1].date()
    while day <= last:
        if (day.day, day.month) == asked:
            return True
        day += timedelta(days=1)
    return False


def _bookable_windows(
    slots: list[DeliverySlot], after_hours: bool, requested_date
) -> list[tuple[datetime, datetime]]:
    windows = []
    for slot in slots:
        start = _utc(slot.start_at).astimezone(IST)
        end = _utc(slot.end_at).astimezone(IST)
        if after_hours and requested_date is not None and start.date() <= requested_date:
            continue
        start = max(start, start.replace(hour=10, minute=0, second=0, microsecond=0))
        end = min(end, end.replace(hour=18, minute=0, second=0, microsecond=0))
        if start < end:
            windows.append((start, end))
    windows.sort()
    merged = []
    for start, end in windows:
        if merged and start.date() == merged[-1][0].date() and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def availability_reply(
    order: Order,
    slots: list[DeliverySlot],
    language: str,
    *,
    rejected=False,
    requested: PlanStep | None = None,
    asked: tuple[int, int] | None = None,
    excluded_dates=(),
    exclude_scheduled_window: bool = False,
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

    alternatives = _bookable_windows(slots, after_hours, requested_date)
    if not alternatives and after_hours:
        # Nothing later is recorded, but the earlier options are still real.
        alternatives = _bookable_windows(slots, False, None)
    alternatives = [window for window in alternatives if window[0].date() not in excluded_dates]
    if exclude_scheduled_window:
        scheduled_start, scheduled_end = _utc(order.window_start), _utc(order.window_end)
        split = []
        for start, end in alternatives:
            if _utc(start) < scheduled_start < _utc(end):
                split.append((start, scheduled_start.astimezone(IST)))
            if _utc(start) < scheduled_end < _utc(end):
                split.append((scheduled_end.astimezone(IST), end))
            if _utc(end) <= scheduled_start or _utc(start) >= scheduled_end:
                split.append((start, end))
        alternatives = split
    matching = [w for w in alternatives if _covers_date(w, asked)] if asked is not None else []
    windows = "; ".join(
        customer_window(start, end, language) for start, end in (matching or alternatives)
    )
    if not windows:
        return {"en-IN": "No alternative delivery availability is recorded. This needs the delivery team's review; no change has been saved.",
                "hi-IN": "डिलीवरी का कोई वैकल्पिक समय दर्ज नहीं है। डिलीवरी टीम की समीक्षा चाहिए; अभी कोई बदलाव नहीं हुआ है।",
                "hi-en": "कोई दूसरा delivery time दर्ज नहीं है। Delivery team की मदद चाहिए, अभी कोई change save नहीं हुआ है।"}[language]
    if asked is not None:
        label = customer_date(asked[0], asked[1], language)
        if matching:
            return {"en-IN": f"{label} is available: {windows}. Which time within it works for you?",
                    "hi-IN": f"{label} उपलब्ध है: {windows}। इसमें कौन सा समय आपके लिए ठीक रहेगा?",
                    "hi-en": f"{windows} में delivery हो सकती है। कौन सा time आपके लिए ठीक रहेगा?"}[language]
        return {"en-IN": f"{label} is not available. Recorded delivery periods are {windows}. Which of these dates and times works for you?",
                "hi-IN": f"{label} उपलब्ध नहीं है। दर्ज डिलीवरी अवधि हैं: {windows}। इनमें कौन सी तारीख और समय आपके लिए ठीक रहेगा?",
                "hi-en": f"{label} available नहीं है। Delivery के options {windows} हैं। इनमें कौन सी date और time ठीक रहेगा?"}[language]
    prefix = {"en-IN": "That requested window is outside this order's delivery availability. ",
              "hi-IN": "माँगा गया समय इस ऑर्डर की उपलब्ध डिलीवरी अवधि में नहीं है। ",
              "hi-en": "आपने जो time बताया, उसमें इस order की delivery available नहीं है। "}[language] if rejected else ""
    filtered = bool(excluded_dates) or exclude_scheduled_window
    limit = {
        "en-IN": "These are the recorded alternatives matching your availability." if filtered else "No other dates are recorded.",
        "hi-IN": "आपकी उपलब्धता के अनुसार ये दर्ज विकल्प हैं।" if filtered else "इनके अलावा कोई तारीख दर्ज नहीं है।",
        "hi-en": "आपकी availability के अनुसार ये दर्ज विकल्प हैं।" if filtered else "इनके अलावा कोई date दर्ज नहीं है।",
    }[language]
    return prefix + {"en-IN": f"Recorded delivery availability is {windows}. {limit} Which date and time works for you?",
                     "hi-IN": f"दर्ज डिलीवरी समय {windows} हैं। {limit} कौन सी तारीख और समय आपके लिए ठीक रहेगा?",
                     "hi-en": f"दर्ज delivery availability {windows} है। {limit} कौन सी date और time आपके लिए ठीक रहेगा?"}[language]


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
