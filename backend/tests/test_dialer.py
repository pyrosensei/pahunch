from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.calls import CallService
from app.errors import DomainError
from app.models import CallOutcome, CallSession, CallState, Order
from app.policy import _utc
from app.seed import seed_database

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path):
    result = create_engine(
        f"sqlite:///{tmp_path / 'dialer.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(result)
    with Session(result) as db:
        seed_database(db)
    yield result
    result.dispose()


def history(db, *, state=CallState.NO_ANSWER, count=1, ended=NOW - timedelta(hours=2)):
    for index in range(count):
        db.add(
            CallSession(
                id=f"previous-{index}",
                order_id=1042,
                trigger_reason="OTP_VERIFIED_DELIVERY",
                state=state,
                outcome=CallOutcome(state.value),
                started_at=ended - timedelta(minutes=10 + index),
                ended_at=ended,
            )
        )
    db.commit()


@pytest.mark.parametrize("count", [3, 4])
def test_manual_call_cannot_bypass_attempt_cap(engine, count):
    with Session(engine) as db:
        history(db, count=count)
        with pytest.raises(DomainError, match="attempt"):
            CallService(db).create_call(1042)


@pytest.mark.parametrize(
    "state",
    [
        CallState.COMMITTED,
        CallState.ESCALATED,
        CallState.TURN_BUDGET_EXHAUSTED,
        CallState.TOOL_FAILURE,
    ],
)
def test_manual_call_cannot_retry_resolved_or_unknown_failure(engine, state):
    with Session(engine) as db:
        history(db, state=state)
        with pytest.raises(DomainError):
            CallService(db).create_call(1042)


def test_manual_call_reserves_global_demo_capacity(engine):
    with Session(engine) as db:
        service = CallService(db)
        service.create_call(1042)
        with pytest.raises(DomainError, match="capacity"):
            service.create_call(1043)


def test_simultaneous_manual_admission_creates_one_attempt(engine):
    barrier = Barrier(2)

    def start():
        with Session(engine) as db:
            barrier.wait(timeout=5)
            try:
                return CallService(db).create_call(1042).id
            except DomainError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(), range(2)))
    assert sum(result is not None for result in results) == 1
    with Session(engine) as db:
        assert len(db.exec(select(CallSession)).all()) == 1


@pytest.fixture
def clock():
    class Clock:
        now = NOW

        def __call__(self):
            return self.now

    return Clock()


@pytest.fixture
def dialer(engine, clock):
    from app.dialer import Dialer

    return Dialer(engine, clock=clock)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"status": "PENDING"}, "NOT_OUT_FOR_DELIVERY"),
        ({"status": "RESCHEDULED"}, "NOT_OUT_FOR_DELIVERY"),
        ({"status": "DELIVERED"}, "ORDER_CLOSED"),
        ({"status": "CANCELLED"}, "ORDER_CLOSED"),
        ({"call_opt_out": True}, "OPTED_OUT"),
        ({"delivery_address_summary": " "}, "MISSING_METADATA"),
        ({"prior_attempts": 0, "requires_otp": False}, "NO_RISK_TRIGGER"),
    ],
)
def test_automatic_skip_reasons(engine, dialer, change, reason):
    with Session(engine) as db:
        order = db.get(Order, 1042)
        for key, value in change.items():
            setattr(order, key, value)
        db.commit()
    dialer.sweep()
    assert (
        next(s for s in dialer.status()["orders"] if s["order_id"] == 1042)["skip_reason"] == reason
    )


@pytest.mark.parametrize(
    "offset,expected", [(-3601, False), (-3600, True), (0, True), (7199, True), (7200, False)]
)
def test_exact_window_boundaries(engine, dialer, clock, offset, expected):
    with Session(engine) as db:
        start = _utc(db.get(Order, 1042).window_start)
    clock.now = start.astimezone(UTC) + timedelta(seconds=offset)
    started = dialer.sweep()
    with Session(engine) as db:
        calls = db.exec(select(CallSession).where(CallSession.order_id == 1042)).all()
        assert bool(calls) == expected
    assert bool(started) == expected


@pytest.mark.parametrize("seconds,expected", [(3599, False), (3600, True), (3601, True)])
def test_retry_gap_uses_end_time_and_exact_boundary(engine, clock, seconds, expected):
    with Session(engine) as db:
        history(db, ended=NOW - timedelta(seconds=seconds))
        service = CallService(db, clock=clock)
        if expected:
            assert service.create_call(1042).id
        else:
            with pytest.raises(DomainError, match="cooldown"):
                service.create_call(1042)


def test_order_completion_timestamp_also_enforces_retry_gap(engine, clock):
    with Session(engine) as db:
        history(db)
        db.get(Order, 1042).last_call_completed_at = NOW - timedelta(minutes=59)
        db.commit()
        with pytest.raises(DomainError, match="cooldown"):
            CallService(db, clock=clock).create_call(1042)


def test_sweeps_do_not_duplicate_events_and_current_state_is_separate(engine, dialer, clock):
    from app.models import DialerEvent, DialerOrderState

    assert len(dialer.sweep()) == 1
    dialer.sweep()
    with Session(engine) as db:
        count = len(db.exec(select(DialerEvent)).all())
        first_checked = db.get(DialerOrderState, 1042).checked_at
    clock.now += timedelta(seconds=5)
    dialer.sweep()
    with Session(engine) as db:
        assert len(db.exec(select(DialerEvent)).all()) == count
        assert db.get(DialerOrderState, 1042).checked_at > first_checked
        assert len(db.exec(select(CallSession)).all()) == 1
        assert (
            len(db.exec(select(DialerEvent).where(DialerEvent.event_type == "STARTED")).all()) == 1
        )


def test_skip_change_and_disposition_are_recorded_once(engine, dialer, clock):
    from app.models import DialerEvent

    sid = dialer.sweep()[0]
    with Session(engine) as db:
        CallService(db, clock=clock).mark_no_answer(sid)
    dialer.sweep()
    dialer.sweep()
    with Session(engine) as db:
        events = db.exec(select(DialerEvent).where(DialerEvent.session_id == sid)).all()
        assert (
            sum(e.event_type == "DISPOSITION_CHANGED" and e.reason == "NO_ANSWER" for e in events)
            == 1
        )
        assert sum(e.event_type == "SKIP_CHANGED" and e.reason == "COOLDOWN" for e in events) == 1


def test_unattached_browser_times_out_and_restart_preserves_attempt(engine, dialer, clock):
    from app.dialer import Dialer

    sid = dialer.sweep()[0]
    clock.now += timedelta(seconds=119)
    assert dialer.sweep() == []
    with Session(engine) as db:
        assert db.get(CallSession, sid).state == CallState.CALLING
    clock.now += timedelta(seconds=1)
    restarted = Dialer(engine, clock=clock)
    restarted.sweep()
    with Session(engine) as db:
        assert db.get(CallSession, sid).state == CallState.NO_ANSWER
        assert len(db.exec(select(CallSession).where(CallSession.order_id == 1042)).all()) == 1


def test_disconnect_grace_allows_reconnect_then_recovers(engine, dialer, clock):
    sid = dialer.sweep()[0]
    with Session(engine) as db:
        calls = CallService(db, clock=clock)
        calls.connect(sid)
        calls.confirm_context(sid)
        calls.browser_presence(sid, attached=True)
        calls.browser_presence(sid, attached=False)
    clock.now += timedelta(seconds=29)
    dialer.sweep()
    with Session(engine) as db:
        assert db.get(CallSession, sid).state == CallState.DISCOVERY
        CallService(db, clock=clock).browser_presence(sid, attached=True)
    dialer.live_sessions.add(sid)
    clock.now += timedelta(minutes=10)
    dialer.sweep()
    with Session(engine) as db:
        assert db.get(CallSession, sid).state == CallState.DISCOVERY
        CallService(db, clock=clock).browser_presence(sid, attached=False)
    dialer.live_sessions.remove(sid)
    clock.now += timedelta(seconds=30)
    dialer.sweep()
    with Session(engine) as db:
        assert db.get(CallSession, sid).state == CallState.UNREACHABLE


def test_abandoned_connected_session_expires(engine, dialer, clock):
    sid = dialer.sweep()[0]
    with Session(engine) as db:
        CallService(db, clock=clock).connect(sid)
    clock.now += timedelta(seconds=300)
    dialer.sweep()
    with Session(engine) as db:
        assert db.get(CallSession, sid).state == CallState.UNREACHABLE


def test_only_explicit_transient_tool_failure_can_retry(engine, clock):
    with Session(engine) as db:
        service = CallService(db, clock=clock)
        call = service.create_call(1042)
        service.finish_call(
            call.id, CallState.TOOL_FAILURE, reason="TRANSIENT_DISPATCH_FAILURE", retryable=True
        )
        clock.now += timedelta(hours=1)
        assert service.create_call(1042).id != call.id


def test_sweep_and_manual_start_share_capacity_and_attempt_lock(engine, dialer, clock):
    barrier = Barrier(2)

    def automatic():
        barrier.wait(timeout=5)
        return dialer.sweep()

    def manual():
        with Session(engine) as db:
            barrier.wait(timeout=5)
            try:
                return CallService(db, clock=clock).create_call(1042).id
            except DomainError:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(automatic), pool.submit(manual)]
        for future in futures:
            future.result(timeout=10)
    with Session(engine) as db:
        assert len(db.exec(select(CallSession)).all()) == 1


def test_lifespan_starts_browser_attempt_without_operator_request(engine, dialer):
    from fastapi.testclient import TestClient

    from app.db import get_session
    from app.main import create_app

    app = create_app(init_database=False, dialer=dialer)

    def sessions():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = sessions
    with TestClient(app) as client:
        with Session(engine) as db:
            call = db.exec(select(CallSession)).one()
            assert call.state == CallState.CALLING
            assert call.customer_confirmed is False
        status = client.get("/api/dialer/status")
        assert status.status_code == 200
        assert status.json()["transport"] == "browser"
        assert status.json()["running"]
        assert client.post("/api/orders/1042/call").status_code == 409
    assert not dialer.running


def test_worker_failure_is_visible_and_shutdown_does_not_leave_task(engine, caplog):
    import asyncio

    from app.dialer import Dialer

    class FailingDialer(Dialer):
        def sweep(self):
            raise RuntimeError("synthetic sweep failure")

    async def run():
        worker = FailingDialer(engine, interval_seconds=0.01)
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        for _ in range(100):
            if worker.last_error:
                break
            await asyncio.sleep(0.005)
        assert worker.status()["last_error"] == "RuntimeError"
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        assert not worker.running

    asyncio.run(run())
    assert "Dialer sweep failed" in caplog.text


@pytest.mark.parametrize(
    "values,trigger",
    [
        ({"requires_otp": True}, "OTP_VERIFIED_DELIVERY"),
        ({"payment_mode": "COD"}, "COD_ORDER"),
        ({"declared_value_inr": 25000}, "HIGH_VALUE_DELIVERY"),
        ({"prior_attempts": 1}, "PRIOR_FAILED_ATTEMPT"),
        ({"declared_value_inr": 24999}, None),
    ],
)
def test_risk_triggers_and_delivery_attempts_are_not_call_attempts(engine, dialer, values, trigger):
    with Session(engine) as db:
        order = db.get(Order, 1042)
        order.prior_attempts = 0
        order.requires_otp = False
        order.declared_value_inr = 0
        for key, value in values.items():
            setattr(order, key, value)
        db.commit()
    dialer.sweep()
    with Session(engine) as db:
        call = db.exec(select(CallSession).where(CallSession.order_id == 1042)).first()
        assert (call.trigger_reason if call else None) == trigger


@pytest.mark.parametrize("state", [CallState.NO_ANSWER, CallState.UNREACHABLE])
def test_retryable_outcomes_and_missing_end_time_use_latest_start(engine, clock, state):
    with Session(engine) as db:
        history(db, state=state)
        call = db.get(CallSession, "previous-0")
        call.ended_at = None
        call.started_at = NOW - timedelta(minutes=59)
        db.commit()
        service = CallService(db, clock=clock)
        with pytest.raises(DomainError, match="cooldown"):
            service.create_call(1042)
        clock.now += timedelta(minutes=1)
        assert service.create_call(1042).id


def test_disconnect_before_answer_does_not_break_sweep(engine, dialer, clock):
    sid = dialer.sweep()[0]
    with Session(engine) as db:
        CallService(db, clock=clock).browser_presence(sid, attached=False)
    clock.now += timedelta(seconds=30)
    dialer.sweep()
    with Session(engine) as db:
        assert db.get(CallSession, sid).state == CallState.NO_ANSWER


def test_terminal_session_is_never_changed_by_late_disconnect(engine, dialer, clock):
    sid = dialer.sweep()[0]
    with Session(engine) as db:
        calls = CallService(db, clock=clock)
        calls.connect(sid)
        calls.confirm_context(sid)
        from app.schemas import CustomerTurnRequest

        calls.submit_turn(
            sid, CustomerTurnRequest(text="Human help please", escalation_reason="REQUESTED_HUMAN")
        )
        calls.browser_presence(sid, attached=False)
    clock.now += timedelta(hours=2)
    dialer.sweep()
    with Session(engine) as db:
        assert db.get(CallSession, sid).state == CallState.ESCALATED


@pytest.mark.parametrize("detached", [False, True])
def test_legacy_created_session_is_recovered_without_deleting_history(
    engine, dialer, clock, detached
):
    with Session(engine) as db:
        db.add(
            CallSession(
                id="legacy",
                order_id=1042,
                trigger_reason="COD_ORDER",
                state=CallState.CREATED,
                started_at=NOW - timedelta(seconds=120),
            )
        )
        db.commit()
        if detached:
            CallService(db, clock=clock).browser_presence("legacy", attached=False)
            clock.now += timedelta(seconds=30)
    dialer.sweep()
    with Session(engine) as db:
        assert db.get(CallSession, "legacy").state == CallState.NO_ANSWER


def test_answer_and_expiry_cannot_overwrite_each_other(engine, dialer, clock, monkeypatch):
    sid = dialer.sweep()[0]
    clock.now += timedelta(seconds=120)
    entered = Event()
    release = Event()
    swept = Event()
    transition = CallService._transition

    def paused_transition(self, call, target):
        transition(self, call, target)
        if target == CallState.CONNECTED:
            entered.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(CallService, "_transition", paused_transition)

    def answer():
        with Session(engine) as db:
            CallService(db, clock=clock).connect(sid)

    def sweep():
        try:
            dialer.sweep()
        finally:
            swept.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        answered = pool.submit(answer)
        assert entered.wait(timeout=5)
        recovering = pool.submit(sweep)
        try:
            assert not swept.wait(timeout=0.2)
        finally:
            release.set()
        answered.result(timeout=5)
        recovering.result(timeout=5)
    with Session(engine) as db:
        call = db.get(CallSession, sid)
        assert call.state == CallState.CONNECTED
        assert call.outcome is None


def test_http_start_and_sweep_use_same_admission(engine, dialer, clock, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.db import get_session

    monkeypatch.setattr(main, "CallService", lambda db: CallService(db, clock=clock))
    app = main.create_app(init_database=False)

    def sessions():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_session] = sessions
    barrier = Barrier(2)

    def automatic():
        barrier.wait(timeout=5)
        return dialer.sweep()

    def manual():
        with TestClient(app) as client:
            barrier.wait(timeout=5)
            return client.post("/api/orders/1042/call").status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        auto = pool.submit(automatic)
        http = pool.submit(manual)
        auto.result(timeout=5)
        assert http.result(timeout=5) in {201, 409}
    with Session(engine) as db:
        assert len(db.exec(select(CallSession)).all()) == 1
