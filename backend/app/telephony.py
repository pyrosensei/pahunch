import base64
import hmac
import re
from urllib.parse import parse_qsl, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlmodel import Session
from starlette.datastructures import FormData

from app.calls import ADMISSION_LOCK, CallService
from app.errors import DomainError
from app.models import CallRuntime, CallState, TwilioBinding
from app.schemas import CustomerTurnRequest
from app.state_machine import TERMINAL_STATES

router = APIRouter(prefix="/twilio")
PROVIDER_TERMINAL = {"completed", "busy", "failed", "no-answer", "canceled"}


def signature(token: str, url: str, params) -> str:
    parts = [url]
    for name in sorted(set(params)):
        values = params.getlist(name) if hasattr(params, "getlist") else [params[name]]
        for value in sorted(set(values)):
            parts.extend((name, value))
    return base64.b64encode(
        hmac.new(token.encode(), "".join(parts).encode(), "sha1").digest()
    ).decode()


def valid_signature(token: str, url: str, params, supplied: str) -> bool:
    return bool(
        token
        and supplied
        and supplied.isascii()
        and hmac.compare_digest(signature(token, url, params), supplied)
    )


class TwilioTransport:
    def __init__(self, engine, settings, *, transport=None):
        self.engine = engine
        self.settings = settings
        self.client = httpx.Client(timeout=15, transport=transport, follow_redirects=False)

    def close(self):
        self.client.close()

    def readiness(self):
        cfg = self.settings
        try:
            url = urlsplit(cfg.public_base_url)
        except ValueError:
            url = urlsplit("")
        valid_url = (
            url.scheme == "https"
            and bool(url.hostname)
            and not (
                url.username or url.password or url.query or url.fragment or url.path.strip("/")
            )
        )
        configured = bool(
            cfg.twilio_account_sid
            and cfg.twilio_auth_token
            and valid_url
            and re.fullmatch(r"\+[1-9]\d{7,14}", cfg.twilio_from_number)
            and re.fullmatch(r"\+[1-9]\d{7,14}", cfg.demo_handset_number)
        )
        return {
            "configured": configured,
            "console_verified": cfg.twilio_console_verified,
            "streaming_verified": cfg.twilio_streaming_verified,
            "gnani_approved": cfg.gnani_phone_demo_approved,
            "session_approval_present": bool(cfg.twilio_approved_session_id),
            "enabled": cfg.telephony_provider == "twilio",
        }

    def url(self, path):
        return self.settings.public_base_url.rstrip("/") + path

    def is_approved(self, session_id):
        return (
            all(self.readiness().values())
            and self.settings.twilio_approved_session_id == session_id
        )

    def dispatch_if_approved(self, session_id):
        cfg = self.settings
        if not self.is_approved(session_id):
            return False
        with ADMISSION_LOCK, Session(self.engine) as db:
            calls = CallService(db)
            call = calls.get_call(session_id)
            binding = db.get(TwilioBinding, session_id)
            if binding is not None:
                if binding.dispatch_state == "REQUESTING":
                    binding.dispatch_state = "UNKNOWN"
                    calls._audit(
                        session_id,
                        "TWILIO_RECONCILIATION_REQUIRED",
                        {"reason": "RESTART_DURING_REQUEST"},
                    )
                    db.commit()
                return False
            if call.state != CallState.CALLING:
                return False
            binding = TwilioBinding(session_id=session_id)
            db.add(binding)
            runtime = db.get(CallRuntime, session_id)
            runtime.transport = "twilio"
            calls._audit(
                session_id,
                "TWILIO_DISPATCH_REQUESTED",
                {"recipient": "owner-handset", "approval": "session-specific"},
            )
            db.commit()
        data = {
            "To": cfg.demo_handset_number,
            "From": cfg.twilio_from_number,
            "Url": self.url(f"/twilio/answer/{session_id}"),
            "Method": "POST",
            "Timeout": "30",
            "StatusCallback": self.url(f"/twilio/status/{session_id}"),
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
        }
        status = None
        call_sid = None
        try:
            response = self.client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{cfg.twilio_account_sid}/Calls.json",
                auth=(cfg.twilio_account_sid, cfg.twilio_auth_token),
                data=data,
            )
            status = response.status_code
            if status == 201:
                body = response.json()
                call_sid = body.get("sid") if isinstance(body, dict) else None
        except (httpx.HTTPError, ValueError):
            # A timed-out POST may already have rung the handset. Keep its reservation.
            status = None
        with ADMISSION_LOCK, Session(self.engine) as db:
            binding = db.get(TwilioBinding, session_id)
            calls = CallService(db)
            if (
                status == 201
                and isinstance(call_sid, str)
                and re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid)
                and binding.call_sid in {None, call_sid}
            ):
                binding.call_sid = call_sid
                binding.dispatch_state = "ACCEPTED"
                calls._audit(session_id, "TWILIO_DISPATCH_ACCEPTED")
            elif status is not None and 400 <= status < 500 and binding.call_sid is None:
                binding.dispatch_state = "REJECTED"
                calls.finish_call(
                    session_id,
                    CallState.TOOL_FAILURE,
                    reason="TWILIO_DISPATCH_REJECTED",
                    retryable=status == 429,
                )
                binding = db.get(TwilioBinding, session_id)
                binding.dispatch_state = "REJECTED"
                calls._audit(session_id, "TWILIO_DISPATCH_REJECTED", {"http_status": status})
            elif binding.call_sid is None:
                binding.dispatch_state = "UNKNOWN"
                calls._audit(session_id, "TWILIO_RECONCILIATION_REQUIRED", {"http_status": status})
            db.commit()
            return binding.dispatch_state == "ACCEPTED"

    def bind_callback(self, db, session_id, params):
        binding = db.get(TwilioBinding, session_id)
        sid = params.get("CallSid", "")
        if (
            binding is None
            or params.get("AccountSid") != self.settings.twilio_account_sid
            or not re.fullmatch(r"CA[0-9a-fA-F]{32}", sid)
        ):
            raise DomainError(403, "Invalid phone callback binding")
        if params.get("To", self.settings.demo_handset_number) != self.settings.demo_handset_number:
            raise DomainError(403, "Invalid phone callback binding")
        if binding.call_sid is None and binding.dispatch_state in {"REQUESTING", "UNKNOWN"}:
            if params.get("To") != self.settings.demo_handset_number:
                raise DomainError(403, "Invalid phone callback binding")
            binding.call_sid = sid
            binding.dispatch_state = "ACCEPTED"
        if binding.call_sid != sid:
            raise DomainError(403, "Invalid phone callback binding")
        db.flush()
        return binding


async def verified_form(request: Request, transport):
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise DomainError(415, "Form-encoded phone callback required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 16384:
            raise DomainError(413, "Phone callback is too large")
    try:
        params = FormData(
            parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=100)
        )
    except (ValueError, UnicodeError) as error:
        raise DomainError(400, "Invalid phone callback") from error
    query = request.url.query
    url = transport.url(request.url.path + ("?" + query if query else ""))
    if not valid_signature(
        transport.settings.twilio_auth_token,
        url,
        params,
        request.headers.get("x-twilio-signature", ""),
    ):
        raise DomainError(403, "Invalid phone callback signature")
    return params


def _transport(request):
    transport = request.app.state.telephony
    if transport is None or not all(
        value for key, value in transport.readiness().items() if key != "session_approval_present"
    ):
        raise DomainError(503, "Phone transport verification is pending")
    return transport


def _xml(root):
    return Response(tostring(root, encoding="unicode"), media_type="application/xml")


@router.post("/answer/{session_id}")
async def answer(session_id: str, request: Request):
    transport = _transport(request)
    params = await verified_form(request, transport)
    root = Element("Response")
    with ADMISSION_LOCK, Session(transport.engine) as db:
        transport.bind_callback(db, session_id, params)
        calls = CallService(db)
        call = calls.get_call(session_id)
        if call.state in TERMINAL_STATES:
            SubElement(root, "Hangup")
        else:
            if call.state == CallState.CALLING:
                calls.connect(session_id)
            gather = SubElement(
                root,
                "Gather",
                input="dtmf",
                numDigits="1",
                timeout="8",
                actionOnEmptyResult="true",
                action=transport.url(f"/twilio/consent/{session_id}"),
                method="POST",
            )
            SubElement(
                gather, "Say"
            ).text = "This is a synthetic SwiftCart demonstration with an automated assistant. Press 1 to agree to speak, or hang up. Do not share any OTP."
            calls._audit(session_id, "PHONE_CONSENT_REQUESTED")
        db.commit()
    return _xml(root)


@router.post("/consent/{session_id}")
async def consent(session_id: str, request: Request):
    transport = _transport(request)
    params = await verified_form(request, transport)
    root = Element("Response")
    with ADMISSION_LOCK, Session(transport.engine) as db:
        binding = transport.bind_callback(db, session_id, params)
        calls = CallService(db)
        call = calls.get_call(session_id)
        if call.state in TERMINAL_STATES:
            SubElement(root, "Hangup")
        elif params.get("Digits") != "1":
            calls.submit_turn(
                session_id,
                CustomerTurnRequest(
                    text="Phone conversation consent declined",
                    escalation_reason="PHONE_CONSENT_DECLINED",
                ),
            )
            SubElement(root, "Say").text = "Thank you. Goodbye."
            SubElement(root, "Hangup")
        else:
            if not binding.consented:
                binding.consented = True
                calls._audit(session_id, "PHONE_CONSENT_ACCEPTED")
                db.commit()
                if call.state == CallState.CONNECTED:
                    calls.confirm_context(session_id)
            connect = SubElement(root, "Connect")
            SubElement(
                connect,
                "Stream",
                url=transport.url(f"/twilio/media/{session_id}").replace("https://", "wss://", 1),
            )
            SubElement(root, "Hangup")
        db.commit()
    return _xml(root)


@router.post("/status/{session_id}")
async def status(session_id: str, request: Request):
    transport = _transport(request)
    params = await verified_form(request, transport)
    try:
        sequence = int(params.get("SequenceNumber", ""))
    except ValueError as error:
        raise DomainError(400, "Invalid callback sequence") from error
    state = params.get("CallStatus")
    if sequence < 0 or state not in PROVIDER_TERMINAL | {
        "queued",
        "initiated",
        "ringing",
        "in-progress",
    }:
        raise DomainError(400, "Invalid callback disposition")
    with ADMISSION_LOCK, Session(transport.engine) as db:
        binding = transport.bind_callback(db, session_id, params)
        if (
            sequence <= binding.last_callback_sequence
            or binding.provider_status in PROVIDER_TERMINAL
        ):
            return Response(status_code=204)
        changed = binding.provider_status != state
        binding.provider_status = state
        binding.last_callback_sequence = sequence
        calls = CallService(db)
        call = calls.get_call(session_id)
        if changed:
            calls._audit(session_id, "TWILIO_STATUS", {"status": state, "sequence": sequence})
        db.commit()
        if state in PROVIDER_TERMINAL and call.state not in TERMINAL_STATES:
            target = (
                CallState.NO_ANSWER if call.state == CallState.CALLING else CallState.UNREACHABLE
            )
            if state == "failed" and call.state == CallState.CALLING:
                target = CallState.TOOL_FAILURE
            calls.finish_call(
                session_id, target, reason="TWILIO_" + state.upper().replace("-", "_")
            )
    return Response(status_code=204)
