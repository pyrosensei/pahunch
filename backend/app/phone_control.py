"""Authenticated, bounded owner-handset automation. Never overrides provider gates."""
from datetime import UTC, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.calls import ADMISSION_LOCK, utc_now
from app.config import settings
from app.db import get_session
from app.errors import DomainError
from app.models import PhoneDemoApproval, PhoneDemoPermit, TwilioBinding
from app.operator import require_operator_key

SessionDep = Annotated[Session, Depends(get_session)]

router = APIRouter(prefix="/api/operator/phone", dependencies=[Depends(require_operator_key)])


class ArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_calls: int = Field(default=1, ge=1, le=3)
    expires_minutes: int = Field(default=15, ge=1, le=30)


def permit_status(db):
    permit = db.get(PhoneDemoPermit, 1)
    armed = bool(permit and not permit.disarmed and permit.remaining_calls > 0 and permit.expires_at.replace(tzinfo=permit.expires_at.tzinfo or UTC) > utc_now())
    return {"armed": armed, "remaining_calls": permit.remaining_calls if armed else 0,
            "expires_at": permit.expires_at if permit else None, "recipient": "configured-owner-handset"}


@router.get("")
def status(request: Request, db: SessionDep):
    transport = request.app.state.telephony
    readiness = transport.readiness() if transport else {
        "enabled": False, "configured": False, "console_verified": settings.twilio_console_verified,
        "streaming_verified": settings.twilio_streaming_verified, "gnani_approved": settings.gnani_phone_demo_approved,
        "session_approval_present": False,
    }
    return {"readiness": readiness, "permit": permit_status(db),
            "blockers": [key for key, value in readiness.items() if not value and key != "session_approval_present"]}


@router.post("/arm")
def arm(body: ArmRequest, request: Request, db: SessionDep):
    transport = request.app.state.telephony
    if transport is None or not all(value for key, value in transport.readiness().items() if key != "session_approval_present"):
        raise DomainError(409, "Phone setup is incomplete. Verify the account, streaming access, public URL and provider approval first.")
    with ADMISSION_LOCK:
        db.expire_all()
        now = utc_now()
        permit = db.get(PhoneDemoPermit, 1)
        if permit is None:
            permit = PhoneDemoPermit(armed_at=now, expires_at=now, remaining_calls=0)
            db.add(permit)
        permit.armed_at = now
        permit.expires_at = now + timedelta(minutes=body.expires_minutes)
        permit.remaining_calls = body.max_calls
        permit.disarmed = False
        db.commit()
        return permit_status(db)


@router.post("/disarm")
def disarm(db: SessionDep):
    with ADMISSION_LOCK:
        for approval in db.exec(select(PhoneDemoApproval)).all():
            if db.get(TwilioBinding, approval.session_id) is None:
                db.delete(approval)
        permit = db.get(PhoneDemoPermit, 1)
        if permit is None:
            permit = PhoneDemoPermit(armed_at=utc_now(), expires_at=utc_now(), remaining_calls=0)
            db.add(permit)
        permit.remaining_calls = 0
        permit.disarmed = True
        db.commit()
        return {**permit_status(db), "note": "Prevents new approvals; an already dispatched call is not hung up."}
