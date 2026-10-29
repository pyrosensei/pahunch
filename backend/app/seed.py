from datetime import datetime

from sqlmodel import Session, select

from app.models import DeliverySlot, Order, OrderStatus, PaymentMode


def at(value: str) -> datetime:
    return datetime.fromisoformat(f"{value}+05:30")


def seed_database(session: Session) -> None:
    if session.exec(select(Order.id).limit(1)).first() is not None:
        return

    orders = [
        Order(
            id=1042,
            merchant_name="SwiftCart",
            delivery_address_summary="Flat 302, Blue Building, behind City Mandir",
            window_start=at("2026-11-05T18:00:00"),
            window_end=at("2026-11-05T20:00:00"),
            payment_mode=PaymentMode.PREPAID,
            requires_otp=True,
            declared_value_inr=4_200,
            safe_drop_allowed=True,
            neighbour_drop_allowed=True,
            security_drop_allowed=True,
            prior_attempts=1,
            status=OrderStatus.OUT_FOR_DELIVERY,
        ),
        Order(
            id=1043,
            merchant_name="SwiftCart",
            delivery_address_summary="House 18, Lake View Road",
            window_start=at("2026-11-05T12:00:00"),
            window_end=at("2026-11-05T15:00:00"),
            payment_mode=PaymentMode.COD,
            declared_value_inr=1_800,
            prior_attempts=0,
            status=OrderStatus.OUT_FOR_DELIVERY,
        ),
        Order(
            id=1044,
            merchant_name="SwiftCart",
            delivery_address_summary="Tower C, Penthouse 2, Cyber Park",
            window_start=at("2026-11-05T18:00:00"),
            window_end=at("2026-11-05T20:00:00"),
            payment_mode=PaymentMode.PREPAID,
            declared_value_inr=52_000,
            safe_drop_allowed=True,
            security_drop_allowed=True,
            status=OrderStatus.OUT_FOR_DELIVERY,
        ),
        Order(
            id=1045,
            merchant_name="SwiftCart",
            delivery_address_summary="Flat 11, Green Apartments, Sector 4",
            window_start=at("2026-11-05T10:00:00"),
            window_end=at("2026-11-05T12:00:00"),
            payment_mode=PaymentMode.PREPAID,
            declared_value_inr=1_200,
            safe_drop_allowed=True,
            security_drop_allowed=True,
            status=OrderStatus.OUT_FOR_DELIVERY,
        ),
        Order(
            id=1046,
            merchant_name="SwiftCart",
            delivery_address_summary="House 42, Rose Street",
            window_start=at("2026-11-05T14:00:00"),
            window_end=at("2026-11-05T16:00:00"),
            payment_mode=PaymentMode.PREPAID,
            declared_value_inr=900,
            safe_drop_allowed=True,
            neighbour_drop_allowed=True,
            status=OrderStatus.OUT_FOR_DELIVERY,
        ),
        Order(
            id=1047,
            merchant_name="SwiftCart",
            delivery_address_summary="",
            window_start=at("2026-11-05T16:00:00"),
            window_end=at("2026-11-05T19:00:00"),
            payment_mode=PaymentMode.PREPAID,
            declared_value_inr=700,
            status=OrderStatus.PENDING,
        ),
        Order(
            id=1048,
            merchant_name="SwiftCart",
            delivery_address_summary="Shop 7, Old Market, near Central Bank",
            window_start=at("2026-11-05T09:00:00"),
            window_end=at("2026-11-05T11:00:00"),
            payment_mode=PaymentMode.PREPAID,
            declared_value_inr=2_400,
            prior_attempts=2,
            status=OrderStatus.OUT_FOR_DELIVERY,
        ),
        Order(
            id=1049,
            merchant_name="SwiftCart",
            delivery_address_summary="Flat 5, River Residency",
            window_start=at("2026-11-04T10:00:00"),
            window_end=at("2026-11-04T12:00:00"),
            payment_mode=PaymentMode.PREPAID,
            declared_value_inr=600,
            status=OrderStatus.DELIVERED,
        ),
    ]
    session.add_all(orders)

    slot_specs = {
        1042: [("2026-11-05T18:00:00", "2026-11-05T20:00:00"), ("2026-11-06T10:00:00", "2026-11-06T12:00:00")],
        1043: [("2026-11-06T12:00:00", "2026-11-06T15:00:00")],
        1044: [("2026-11-05T18:00:00", "2026-11-05T20:00:00")],
        1045: [("2026-11-05T18:00:00", "2026-11-05T20:00:00")],
        1046: [("2026-11-05T18:00:00", "2026-11-05T20:00:00")],
        1048: [("2026-11-06T09:00:00", "2026-11-06T11:00:00")],
    }
    for order_id, windows in slot_specs.items():
        for start, end in windows:
            session.add(DeliverySlot(order_id=order_id, start_at=at(start), end_at=at(end)))
    session.commit()
