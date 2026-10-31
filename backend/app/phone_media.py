import asyncio
import base64
import binascii
import json
import re
from collections import deque
from dataclasses import replace

from fastapi import APIRouter, WebSocket
from sqlmodel import Session
from starlette.websockets import WebSocketDisconnect

from app.calls import ADMISSION_LOCK, CallService, utc_now
from app.errors import DomainError
from app.models import CallRuntime, CallSession, CallState, TwilioBinding
from app.phone_audio import TelephoneSpeech, decode_mulaw, encode_mulaw
from app.realtime import handle_voice_socket
from app.schemas import CustomerTurnRequest
from app.state_machine import TERMINAL_STATES
from app.telephony import valid_signature

router = APIRouter()


class PhoneSocket:
    def __init__(self, websocket, stream_sid, audit, *, on_turn=None):
        self.websocket = websocket
        self.stream_sid = stream_sid
        self.audit = audit
        self.on_turn = on_turn
        self.controls = deque(
            [
                {
                    "type": "config",
                    "tts_sample_rate": 8000,
                    "playback_tracking": True,
                    "audio_feedback": True,
                },
                {"type": "start"},
            ]
        )
        self.input_buffer = bytearray()
        self.marks = {}
        self.generation = 0
        self.cancelled = -1
        self.last_sequence = 0
        self.terminal = False
        self.closed = False

    async def close(self, code=1000):
        if not self.closed:
            self.closed = True
            await self.websocket.close(code=code)

    async def receive(self):
        while not self.closed:
            if self.controls:
                return {"type": "websocket.receive", "text": json.dumps(self.controls.popleft())}
            if len(self.input_buffer) >= 1024:
                frame = bytes(self.input_buffer[:1024])
                del self.input_buffer[:1024]
                return {"type": "websocket.receive", "bytes": frame}
            raw = await self.websocket.receive()
            if raw["type"] == "websocket.disconnect":
                return raw
            text = raw.get("text")
            if not isinstance(text, str) or len(text) > 16384:
                raise DomainError(400, "Invalid phone media envelope")
            try:
                event = json.loads(text)
            except ValueError as error:
                raise DomainError(400, "Invalid phone media envelope") from error
            if not isinstance(event, dict) or event.get("streamSid") != self.stream_sid:
                raise DomainError(403, "Invalid phone stream binding")
            kind = event.get("event")
            if kind == "stop":
                return {"type": "websocket.disconnect", "code": 1000}
            if kind == "mark":
                mark = event.get("mark")
                if not isinstance(mark, dict):
                    raise DomainError(400, "Invalid phone playback mark")
                name = mark.get("name")
                gen = self.marks.pop(name, None)
                if gen is not None and gen > self.cancelled:
                    if self.terminal:
                        await self.close()
                        break
                    self.controls.append({"type": "start"})
                    return {
                        "type": "websocket.receive",
                        "text": json.dumps({"type": "playback_done", "generation_id": gen}),
                    }
                continue
            if kind == "dtmf":
                self.audit("PHONE_DTMF_IGNORED")
                continue
            if kind != "media":
                raise DomainError(400, "Unexpected phone media event")
            try:
                sequence = int(event.get("sequenceNumber", ""))
            except (ValueError, TypeError) as error:
                raise DomainError(400, "Invalid phone media sequence") from error
            if sequence <= self.last_sequence:
                self.audit("PHONE_MEDIA_REPLAY_DROPPED")
                continue
            self.last_sequence = sequence
            media = event.get("media", {})
            if not isinstance(media, dict):
                raise DomainError(400, "Invalid phone audio payload")
            payload = media.get("payload")
            if (
                media.get("track") != "inbound"
                or not isinstance(payload, str)
                or not 0 < len(payload) <= 4096
            ):
                raise DomainError(400, "Invalid phone audio payload")
            try:
                pcm = decode_mulaw(base64.b64decode(payload, validate=True))
            except (ValueError, binascii.Error) as error:
                raise DomainError(400, "Invalid phone audio payload") from error
            if not self.terminal:
                self.input_buffer.extend(pcm)
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_json(self, event):
        kind = event.get("type")
        gen = event.get("generation_id", 0)
        if kind == "turn" and self.on_turn is not None:
            self.terminal = await self.on_turn(event)
        elif kind == "stop_speaking":
            self.cancelled = max(self.cancelled, gen)
            self.marks = {
                name: value for name, value in self.marks.items() if value > self.cancelled
            }
            await self.websocket.send_json({"event": "clear", "streamSid": self.stream_sid})
        elif kind in {"agent_audio_chunk", "agent_audio_end"}:
            if gen <= self.cancelled or gen < self.generation:
                return
            self.generation = gen
            if kind == "agent_audio_chunk":
                if event.get("sample_rate") != 8000:
                    raise DomainError(400, "Invalid telephone output sample rate")
                pcm = base64.b64decode(event["chunk_base64"], validate=True)
                data = encode_mulaw(pcm)
                for offset in range(0, len(data), 160):
                    await self.websocket.send_json(
                        {
                            "event": "media",
                            "streamSid": self.stream_sid,
                            "media": {
                                "payload": base64.b64encode(data[offset : offset + 160]).decode()
                            },
                        }
                    )
            else:
                if event.get("audio_error"):
                    self.audit("PHONE_AUDIO_FAILED", {"error": event["audio_error"]})
                    self.terminal = True
                name = f"pahunch-{gen}"
                self.marks[name] = gen
                await self.websocket.send_json(
                    {"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}}
                )
        elif kind in {"error", "agent_audio"}:
            self.audit("PHONE_ENGINE_FAILED", {"reason": "VOICE_OUTPUT_UNAVAILABLE"})
            await self.close(code=1011)


@router.websocket("/twilio/media/{session_id}")
async def phone_media(websocket: WebSocket, session_id: str):
    await websocket.accept()
    transport = websocket.app.state.telephony
    if transport is None:
        await websocket.close(code=4403)
        return
    cfg = transport.settings
    readiness = transport.readiness()
    url = transport.url(websocket.url.path).replace("https://", "wss://", 1)
    if not all(
        value for key, value in readiness.items() if key != "session_approval_present"
    ) or not valid_signature(
        cfg.twilio_auth_token, url, {}, websocket.headers.get("x-twilio-signature", "")
    ):
        await websocket.close(code=4403)
        return
    owners = websocket.app.state.voice_owners
    if session_id in owners:
        await websocket.close(code=4409)
        return
    owners.add(session_id)
    started = False

    def audit(kind, payload=None):
        with Session(transport.engine) as db:
            if db.get(CallSession, session_id) is not None:
                CallService(db)._audit(session_id, kind, payload)
                db.commit()

    async def on_turn(event):
        with Session(transport.engine) as db:
            calls = CallService(db)
            if event.get("turn", {}).get("customer_confirmed"):
                try:
                    calls.commit_plan(session_id, "phone-" + session_id)
                except DomainError as error:
                    audit("PHONE_COMMIT_FAILED", {"status": error.status_code})
                    call = calls.get_call(session_id)
                    if call.state not in TERMINAL_STATES:
                        calls.submit_turn(
                            session_id,
                            CustomerTurnRequest(
                                text="Phone commit needs review",
                                escalation_reason="PHONE_COMMIT_FAILED",
                            ),
                        )
            return calls.get_call(session_id).state in TERMINAL_STATES

    try:
        with Session(transport.engine) as db:
            binding = db.get(TwilioBinding, session_id)
            if binding is None or not binding.consented or binding.stream_sid is not None:
                raise DomainError(403, "Phone stream is not awaiting attachment")

        async def read_start():
            message = await asyncio.wait_for(websocket.receive(), timeout=5)
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect()
            text = message.get("text")
            if not isinstance(text, str) or len(text) > 16384:
                raise DomainError(400, "Invalid phone stream envelope")
            value = json.loads(text)
            if not isinstance(value, dict):
                raise DomainError(400, "Invalid phone stream envelope")
            return value

        raw = await read_start()
        if raw.get("event") == "connected":
            raw = await read_start()
        start = raw.get("start", {})
        if not isinstance(start, dict):
            raise DomainError(400, "Invalid phone stream start")
        sid = start.get("streamSid")
        if (
            raw.get("event") != "start"
            or start.get("accountSid") != cfg.twilio_account_sid
            or not isinstance(sid, str)
            or not re.fullmatch(r"MZ[0-9a-fA-F]{32}", sid)
            or raw.get("streamSid") != sid
            or start.get("mediaFormat")
            != {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}
        ):
            raise DomainError(403, "Invalid phone stream start")
        with ADMISSION_LOCK, Session(transport.engine) as db:
            binding = db.get(TwilioBinding, session_id)
            calls = CallService(db)
            call = calls.get_call(session_id)
            if binding.call_sid != start.get("callSid") or call.state not in {
                CallState.DISCOVERY,
                CallState.NEGOTIATION,
                CallState.PENDING_CONFIRMATION,
            }:
                raise DomainError(403, "Invalid phone stream call binding")
            binding.stream_sid = sid
            db.get(CallRuntime, session_id).last_seen_at = utc_now()
            calls._audit(session_id, "TWILIO_STREAM_STARTED", {"sample_rate": 8000})
            db.commit()
            started = True
        speech = websocket.app.state.phone_speech
        llm = websocket.app.state.phone_llm
        if speech is None or llm is None:
            raise DomainError(503, "Phone speech is not configured")
        adapter = PhoneSocket(websocket, sid, audit, on_turn=on_turn)
        with Session(transport.engine) as db:
            await handle_voice_socket(
                adapter,
                session_id,
                db,
                llm,
                TelephoneSpeech(speech),
                replace(websocket.app.state.phone_config, sample_rate=8000),
                streaming=websocket.app.state.phone_streaming_factory(),
            )
    except WebSocketDisconnect:
        if started:
            audit("TWILIO_STREAM_DISCONNECTED")
    except (DomainError, ValueError, TimeoutError) as error:
        audit("TWILIO_STREAM_REJECTED", {"error_type": type(error).__name__})
        await websocket.close(code=4403)
    finally:
        owners.discard(session_id)
        if started:
            with ADMISSION_LOCK, Session(transport.engine) as db:
                calls = CallService(db)
                call = calls.get_call(session_id)
                calls._audit(session_id, "TWILIO_STREAM_ENDED")
                db.commit()
                if call.state not in TERMINAL_STATES:
                    calls.finish_call(
                        session_id, CallState.UNREACHABLE, reason="TWILIO_STREAM_ENDED"
                    )
