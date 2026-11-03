from datetime import UTC, datetime, timedelta, timezone

from sqlmodel import Session, SQLModel, create_engine

from app.models import DeliverySlot, Order, PaymentMode
from app.policy import _utc


def test_sqlite_roundtrip_preserves_delivery_instant_in_api_and_policy():
    ist = timezone(timedelta(hours=5, minutes=30))
    start = datetime(2026, 11, 2, 18, 15, tzinfo=ist)
    engine = create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Order(id=1, merchant_name='SwiftCart', delivery_address_summary='Synthetic address',
                     window_start=start, window_end=start + timedelta(hours=2), payment_mode=PaymentMode.COD))
        db.add(DeliverySlot(order_id=1, start_at=start, end_at=start + timedelta(hours=2)))
        db.commit()
        order = db.get(Order, 1)
        slot = db.get(DeliverySlot, 1)
        api_start = datetime.fromisoformat(order.model_dump(mode='json')['window_start'])
        api_slot = datetime.fromisoformat(slot.model_dump(mode='json')['start_at'])
        assert api_start == api_slot == start
        assert api_start.astimezone(UTC) == _utc(order.window_start)
    engine.dispose()
