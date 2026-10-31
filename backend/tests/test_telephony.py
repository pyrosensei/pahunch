import json
import struct

import httpx
import pytest
from sqlmodel import Session, select
from starlette.datastructures import FormData
from test_dialer import engine as dialer_engine

from app.calls import CallService
from app.config import Settings
from app.errors import DomainError
from app.models import AuditEvent, CallSession, CallState

engine = dialer_engine


@pytest.fixture
def phone(engine):
    from app.telephony import TwilioTransport

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"sid": "CA" + "1" * 32})

    config = Settings(
        _env_file=None,
        telephony_provider="twilio",
        twilio_account_sid="AC" + "2" * 32,
        twilio_auth_token="synthetic-test-token",
        twilio_from_number="+15005550006",
        demo_handset_number="+15005550001",
        public_base_url="https://phone.example",
        twilio_console_verified=True,
        twilio_streaming_verified=True,
        gnani_phone_demo_approved=True,
    )
    with Session(engine) as db:
        sid = CallService(db).create_call(1042).id
    config.twilio_approved_session_id = sid
    transport = TwilioTransport(engine, config, transport=httpx.MockTransport(handler))
    yield transport, config, sid, requests
    transport.close()


@pytest.mark.parametrize(
    "gate", ["twilio_console_verified", "twilio_streaming_verified", "gnani_phone_demo_approved"]
)
def test_phone_dispatch_needs_all_verification_gates(phone, gate):
    transport, config, sid, requests = phone
    setattr(config, gate, False)
    assert not transport.dispatch_if_approved(sid)
    assert requests == []


def test_phone_approval_is_for_one_session_and_dispatch_is_not_repeated(phone):
    transport, config, sid, requests = phone
    assert not transport.dispatch_if_approved("another-session")
    assert transport.dispatch_if_approved(sid)
    assert not transport.dispatch_if_approved(sid)
    assert len(requests) == 1
    assert b"Record" not in requests[0].content
    with Session(transport.engine) as db:
        assert db.get(CallSession, sid).state == CallState.CALLING
    serialized = json.dumps(transport.readiness())
    for value in [config.twilio_auth_token, config.demo_handset_number, config.public_base_url]:
        assert value not in serialized


@pytest.mark.parametrize("status", [400, 429, 500])
def test_dispatch_failure_is_explicit_and_never_blindly_repeated(phone, status):
    from app.models import TwilioBinding

    transport, _, sid, requests = phone
    transport.client.close()

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"message": "synthetic sensitive provider body"})

    transport.client = httpx.Client(transport=httpx.MockTransport(handler))
    transport.dispatch_if_approved(sid)
    transport.dispatch_if_approved(sid)
    assert len(requests) == 1
    with Session(transport.engine) as db:
        binding = db.get(TwilioBinding, sid)
        assert binding.dispatch_state == ("UNKNOWN" if status >= 500 else "REJECTED")
        stored = " ".join(e.payload_json for e in db.exec(select(AuditEvent)).all())
        assert "sensitive provider body" not in stored


def test_ambiguous_create_timeout_keeps_capacity_and_needs_reconciliation(phone):
    from app.models import TwilioBinding

    transport, _, sid, requests = phone
    transport.client.close()

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("synthetic timeout")

    transport.client = httpx.Client(transport=httpx.MockTransport(handler))
    transport.dispatch_if_approved(sid)
    transport.dispatch_if_approved(sid)
    assert len(requests) == 1
    with Session(transport.engine) as db:
        assert db.get(TwilioBinding, sid).dispatch_state == "UNKNOWN"
        assert db.get(CallSession, sid).state == CallState.CALLING
        with pytest.raises(DomainError, match="capacity"):
            CallService(db).create_call(1043)


def test_signature_matches_official_twilio_duplicate_parameter_vector():
    from app.telephony import signature, valid_signature

    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = FormData(
        [
            ("Sid", "CA123"),
            ("SidAccount", "AC123"),
            ("Digits", "5678"),
            ("Digits", "1234"),
            ("Digits", "1234"),
        ]
    )
    expected = "IK+Dwps556ElfBT0I3Rgjkr1wJU="
    assert signature("12345", url, params) == expected
    assert valid_signature("12345", url, params, expected)
    assert not valid_signature("12345", url + "x", params, expected)


@pytest.mark.parametrize(
    "value,sample", [(255, 0), (127, 0), (0, -32124), (128, 32124), (254, 8), (126, -8)]
)
def test_mulaw_reference_vectors(value, sample):
    from app.phone_audio import decode_mulaw, encode_mulaw

    assert decode_mulaw(bytes([value])) == struct.pack("<h", sample)
    assert encode_mulaw(struct.pack("<h", sample)) == bytes([255 if sample == 0 else value])


def test_mulaw_roundtrip_and_invalid_pcm_length():
    from app.phone_audio import decode_mulaw, encode_mulaw

    data = bytes(range(256))
    assert encode_mulaw(decode_mulaw(data)) == data[:127] + bytes([255]) + data[128:]
    with pytest.raises(ValueError):
        encode_mulaw(b"x")


def test_native_mulaw_tts_stream_and_blocking_fallback_keep_language():
    import asyncio

    from app.phone_audio import TelephoneSpeech

    class Speech:
        def __init__(self):
            self.calls = []

        async def stream_pcm(self, text, **kwargs):
            self.calls.append(kwargs)
            yield b"\xff\xfe"

        async def synthesize_pcm_blocking(self, text, **kwargs):
            self.calls.append(kwargs)
            return b"\xff\xfe"

    async def run():
        speech = Speech()
        proxy = TelephoneSpeech(speech)
        assert (
            b"".join(
                [c async for c in proxy.stream_pcm("Hello", sample_rate=8000, language="en-IN")]
            )
            == b"\0\0\x08\0"
        )
        assert (
            await proxy.synthesize_pcm_blocking("Hello", sample_rate=8000, language="en-IN")
            == b"\0\0\x08\0"
        )
        assert all(
            c == {"sample_rate": 8000, "language": "en-IN", "container": "mulaw"}
            for c in speech.calls
        )

    asyncio.run(run())


@pytest.fixture
def phone_client(phone):
    from fastapi.testclient import TestClient

    from app.main import create_app

    transport, _, sid, _ = phone
    transport.dispatch_if_approved(sid)
    app = create_app(init_database=False, speech_client=None, telephony=transport)
    with TestClient(app) as client:
        yield client


def signed_post(phone, client, route, **extra):
    from app.telephony import signature

    transport, cfg, sid, _ = phone
    params = {
        "AccountSid": cfg.twilio_account_sid,
        "CallSid": "CA" + "1" * 32,
        "To": cfg.demo_handset_number,
        **extra,
    }
    path = f"/twilio/{route}/{sid}"
    return client.post(
        path,
        data=params,
        headers={
            "X-Twilio-Signature": signature(cfg.twilio_auth_token, transport.url(path), params)
        },
    )


def test_unsigned_or_mismatched_callbacks_are_rejected(phone, phone_client):
    _, _, sid, _ = phone
    assert phone_client.post(f"/twilio/answer/{sid}", data={"CallSid": "other"}).status_code == 403
    assert signed_post(phone, phone_client, "answer", AccountSid="ACwrong").status_code == 403
    assert signed_post(phone, phone_client, "answer", CallSid="CA" + "3" * 32).status_code == 403


def test_answer_requests_conversation_consent_before_starting_stream(phone, phone_client):
    response = signed_post(phone, phone_client, "answer")
    assert response.status_code == 200
    assert "<Gather" in response.text and "Press 1" in response.text
    assert "<Stream" not in response.text
    assert "<Record" not in response.text
    assert signed_post(phone, phone_client, "consent", Digits="1").status_code == 200
    accepted = signed_post(phone, phone_client, "consent", Digits="1")
    assert "<Stream" in accepted.text
    with Session(phone[0].engine) as db:
        call = db.get(CallSession, phone[2])
        assert call.state == CallState.DISCOVERY and not call.customer_confirmed
        assert (
            sum(e.event_type == "PHONE_CONSENT_ACCEPTED" for e in db.exec(select(AuditEvent)).all())
            == 1
        )


@pytest.mark.parametrize("digits", ["", "0", "9", "123456"])
def test_refused_or_ambiguous_phone_consent_ends_without_collecting_digits(
    phone, phone_client, digits
):
    signed_post(phone, phone_client, "answer")
    response = signed_post(phone, phone_client, "consent", Digits=digits)
    assert "<Hangup" in response.text and "<Stream" not in response.text
    with Session(phone[0].engine) as db:
        assert db.get(CallSession, phone[2]).state == CallState.ESCALATED
        assert (
            all(digits not in e.payload_json for e in db.exec(select(AuditEvent)).all())
            if len(digits) > 1
            else True
        )


def test_callbacks_ignore_duplicates_reordering_and_terminal_regressions(phone, phone_client):
    assert (
        signed_post(
            phone, phone_client, "status", CallStatus="ringing", SequenceNumber="0"
        ).status_code
        == 204
    )
    assert (
        signed_post(
            phone, phone_client, "status", CallStatus="ringing", SequenceNumber="0"
        ).status_code
        == 204
    )
    assert (
        signed_post(
            phone, phone_client, "status", CallStatus="completed", SequenceNumber="2"
        ).status_code
        == 204
    )
    for sequence in ("1", "3"):
        assert (
            signed_post(
                phone, phone_client, "status", CallStatus="in-progress", SequenceNumber=sequence
            ).status_code
            == 204
        )
    with Session(phone[0].engine) as db:
        assert db.get(CallSession, phone[2]).state == CallState.NO_ANSWER
        assert sum(e.event_type == "TWILIO_STATUS" for e in db.exec(select(AuditEvent)).all()) == 2


def test_provider_completion_cannot_overwrite_committed_plan(phone, phone_client):
    from app.models import CallOutcome

    with Session(phone[0].engine) as db:
        call = db.get(CallSession, phone[2])
        call.state = CallState.COMMITTED
        call.outcome = CallOutcome.COMMITTED
        db.commit()
    assert (
        signed_post(
            phone, phone_client, "status", CallStatus="completed", SequenceNumber="4"
        ).status_code
        == 204
    )
    with Session(phone[0].engine) as db:
        assert db.get(CallSession, phone[2]).outcome == CallOutcome.COMMITTED


def test_provider_reserved_call_is_not_expired_by_browser_timeout(phone):
    from datetime import UTC, datetime

    from app.dialer import Dialer

    transport, _, sid, requests = phone
    transport.dispatch_if_approved(sid)
    dialer = Dialer(transport.engine, clock=lambda: datetime(2026, 11, 5, 12, tzinfo=UTC))
    assert dialer.sweep() == []
    assert dialer.sweep() == []
    with Session(transport.engine) as db:
        assert db.get(CallSession, sid).state == CallState.CALLING
    assert len(requests) == 1


@pytest.mark.parametrize("method", ["stream_pcm", "synthesize_pcm_blocking"])
def test_gnani_native_phone_request_uses_mulaw_at_8000_hz(method):
    import asyncio
    import base64

    from app.gnani import GnaniSpeechClient

    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        if request.url.path.endswith("/sse"):
            return httpx.Response(
                200,
                text="data: " + json.dumps({"audio": base64.b64encode(b"\xff").decode()}) + "\n\n",
            )
        return httpx.Response(200, content=b"\xff")

    async def run():
        speech = GnaniSpeechClient(
            api_key="synthetic-test-key", transport=httpx.MockTransport(handler)
        )
        try:
            if method == "stream_pcm":
                data = b"".join(
                    [
                        c
                        async for c in speech.stream_pcm(
                            "Hello", sample_rate=8000, language="en-IN", container="mulaw"
                        )
                    ]
                )
            else:
                data = await speech.synthesize_pcm_blocking(
                    "Hello", sample_rate=8000, language="en-IN", container="mulaw"
                )
            assert data == b"\xff"
        finally:
            await speech.aclose()

    asyncio.run(run())
    assert captured[0]["audio_config"] == {"container": "mulaw", "sample_rate": 8000}
    assert captured[0]["language"] == "en-IN"


@pytest.mark.parametrize("method", ["synthesize_pcm", "synthesize_pcm_blocking"])
def test_empty_phone_synthesis_is_not_cached_or_counted_as_audio(method):
    import asyncio

    from app.gnani import GnaniResponseError
    from app.phone_audio import TelephoneSpeech

    class EmptySpeech:
        async def stream_pcm(self, *args, **kwargs):
            if False:
                yield b""

        async def synthesize_pcm_blocking(self, *args, **kwargs):
            return b""

    async def run():
        speech = TelephoneSpeech(EmptySpeech())
        with pytest.raises(GnaniResponseError):
            await getattr(speech, method)("Hello", sample_rate=8000, language="en-IN")
        assert speech._acks == {}

    asyncio.run(run())
