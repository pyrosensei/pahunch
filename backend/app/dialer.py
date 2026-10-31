import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine
from sqlmodel import Session, select

from app.calls import ADMISSION_LOCK, CallService, utc_now
from app.models import (
    CallRuntime,
    CallSession,
    CallState,
    DialerEvent,
    DialerOrderState,
    Order,
    Turn,
    TwilioBinding,
)
from app.state_machine import TERMINAL_STATES

logger = logging.getLogger(__name__)


def _call_time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Dialer:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = utc_now,
        interval_seconds: float = 5,
        waiting_timeout_seconds: float = 120,
        disconnect_grace_seconds: float = 30,
        idle_timeout_seconds: float = 300,
        live_sessions: set[str] | None = None,
        telephony=None,
    ):
        self.engine = engine
        self.clock = clock
        self.interval_seconds = interval_seconds
        self.waiting_timeout = timedelta(seconds=waiting_timeout_seconds)
        self.disconnect_grace = timedelta(seconds=disconnect_grace_seconds)
        self.idle_timeout = timedelta(seconds=idle_timeout_seconds)
        self.live_sessions = live_sessions if live_sessions is not None else set()
        self.running = False
        self.last_sweep_at: datetime | None = None
        self.last_error: str | None = None
        self.telephony = telephony

    def _event(self, db, call, kind, reason, payload=None):
        db.add(
            DialerEvent(
                order_id=call.order_id,
                session_id=call.id,
                event_type=kind,
                reason=reason,
                payload_json=json.dumps(payload or {}),
                created_at=self.clock(),
            )
        )

    def _recover(self, db: Session, calls: CallService) -> None:
        now = self.clock()
        for call in db.exec(select(CallSession)).all():
            runtime = db.get(CallRuntime, call.id)
            if runtime is None:
                runtime = CallRuntime(
                    session_id=call.id,
                    last_seen_at=call.started_at,
                    observed_state=CallState.CREATED,
                )
                db.add(runtime)
                db.flush()
            if call.state not in TERMINAL_STATES:
                binding = db.get(TwilioBinding, call.id)
                protected = binding is not None and binding.dispatch_state in {
                    "REQUESTING",
                    "ACCEPTED",
                    "UNKNOWN",
                }
                approved = self.telephony is not None and self.telephony.is_approved(call.id)
                if protected or approved:
                    # Provider callbacks or reconciliation must resolve a possibly live call.
                    pass
                elif call.id in self.live_sessions:
                    runtime.last_seen_at = now
                    runtime.disconnected_at = None
                else:
                    latest_turn = db.exec(
                        select(Turn)
                        .where(Turn.session_id == call.id)
                        .order_by(Turn.created_at.desc())
                    ).first()
                    last_seen = max(
                        _call_time(runtime.last_seen_at),
                        _call_time(latest_turn.created_at if latest_turn else call.started_at),
                    )
                    disconnected = runtime.disconnected_at
                    if disconnected and now - _call_time(disconnected) >= self.disconnect_grace:
                        if call.state == CallState.CREATED:
                            calls._transition(call, CallState.CALLING)
                            db.commit()
                        target = (
                            CallState.NO_ANSWER
                            if call.state == CallState.CALLING
                            else CallState.UNREACHABLE
                        )
                        calls.finish_call(call.id, target, reason="BROWSER_DISCONNECT_TIMEOUT")
                    elif call.state in {CallState.CREATED, CallState.CALLING}:
                        if now - _call_time(call.started_at) >= self.waiting_timeout:
                            if call.state == CallState.CREATED:
                                calls._transition(call, CallState.CALLING)
                                db.commit()
                            calls.finish_call(
                                call.id, CallState.NO_ANSWER, reason="BROWSER_ATTACH_TIMEOUT"
                            )
                    elif now - last_seen >= self.idle_timeout:
                        calls.finish_call(
                            call.id, CallState.UNREACHABLE, reason="ABANDONED_SESSION_TIMEOUT"
                        )
            if (runtime.observed_state, runtime.observed_outcome) != (call.state, call.outcome):
                self._event(
                    db,
                    call,
                    "DISPOSITION_CHANGED",
                    call.state.value,
                    {
                        "previous_state": runtime.observed_state.value,
                        "outcome": call.outcome.value if call.outcome else None,
                    },
                )
                runtime.observed_state = call.state
                runtime.observed_outcome = call.outcome
        db.commit()

    def sweep(self) -> list[str]:
        started = []
        with ADMISSION_LOCK, Session(self.engine) as db:
            calls = CallService(db, clock=self.clock)
            self._recover(db, calls)
            order_ids = list(db.exec(select(Order.id).order_by(Order.window_start, Order.id)).all())
            for order_id in order_ids:
                db.expire_all()
                order = db.get(Order, order_id)
                reason = calls.eligibility_reason(order, automatic=True)
                if reason is None:
                    call = calls.create_call(order_id, automatic=True)
                    started.append(call.id)
                    reason = "ACTIVE_CALL"
                latest = db.exec(
                    select(CallSession)
                    .where(CallSession.order_id == order_id)
                    .order_by(CallSession.started_at.desc())
                ).first()
                sid = latest.id if latest else None
                state = db.get(DialerOrderState, order_id)
                if state is None or state.skip_reason != reason:
                    db.add(
                        DialerEvent(
                            order_id=order_id,
                            session_id=sid,
                            event_type="SKIP_CHANGED",
                            reason=reason,
                            created_at=self.clock(),
                        )
                    )
                if state is None:
                    state = DialerOrderState(
                        order_id=order_id, skip_reason=reason, checked_at=self.clock()
                    )
                    db.add(state)
                state.skip_reason = reason
                state.session_id = sid
                state.checked_at = self.clock()
                db.commit()
        self.last_sweep_at = self.clock()
        self.last_error = None
        if self.telephony is not None:
            with Session(self.engine) as db:
                waiting = list(
                    db.exec(
                        select(CallSession.id).where(CallSession.state == CallState.CALLING)
                    ).all()
                )
            for session_id in waiting:
                self.telephony.dispatch_if_approved(session_id)
        return started

    def status(self) -> dict:
        with Session(self.engine) as db:
            orders = [
                s.model_dump(mode="json")
                for s in db.exec(select(DialerOrderState).order_by(DialerOrderState.order_id)).all()
            ]
        return {
            "running": self.running,
            "transport": "twilio"
            if self.telephony is not None and self.telephony.readiness()["enabled"]
            else "browser",
            "last_sweep_at": self.last_sweep_at,
            "last_error": self.last_error,
            "orders": orders,
            "phone": self.telephony.readiness() if self.telephony is not None else None,
        }

    async def run(self, stop: asyncio.Event) -> None:
        self.running = True
        try:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(self.sweep)
                except Exception as error:
                    self.last_error = type(error).__name__
                    logger.exception("Dialer sweep failed: %s", self.last_error)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    pass
        finally:
            self.running = False
