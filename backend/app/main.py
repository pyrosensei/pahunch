from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, cast

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app.calls import CallService
from app.config import settings
from app.db import get_session, init_db
from app.errors import DomainError
from app.gnani import SpeechClient, build_gnani_speech_client
from app.llm import StructuredLLM, build_llm_client
from app.models import AuditEvent, CallSession, DeliverySlot, Order
from app.planner import PlannerService
from app.policy import validate_plan
from app.realtime import RealtimeConfig, build_realtime_config, handle_voice_socket
from app.schemas import (
    AgentTurnRequest,
    AgentTurnResponse,
    CommitRequest,
    CommitResult,
    CourierCard,
    CustomerTurnRequest,
    PlanValidationRequest,
    PolicyVerdict,
    TurnResult,
    VoiceTurnResponse,
)
from app.streaming_stt import StreamingSTTClient, build_streaming_stt_client
from app.voice import MAX_AUDIO_BYTES, VoiceTurnService

SessionDep = Annotated[Session, Depends(get_session)]
_SPEECH_UNSET = object()


def create_app(
    *,
    init_database: bool = True,
    llm_client: StructuredLLM | None = None,
    speech_client: SpeechClient | None | object = _SPEECH_UNSET,
    streaming_stt_client: StreamingSTTClient | None | object = _SPEECH_UNSET,
    max_audio_bytes: int = MAX_AUDIO_BYTES,
    realtime_config: RealtimeConfig | None = None,
) -> FastAPI:
    planner_llm = llm_client if llm_client is not None else build_llm_client(settings)
    speech = (
        build_gnani_speech_client(settings)
        if speech_client is _SPEECH_UNSET
        else cast(SpeechClient | None, speech_client)
    )
    def new_streaming_stt() -> StreamingSTTClient | None:
        # One Prisma stream per voice connection: client state (_queue,
        # _partial, _last_error) belongs to a single live session.
        if streaming_stt_client is _SPEECH_UNSET:
            return build_streaming_stt_client(settings)
        return cast(StreamingSTTClient | None, streaming_stt_client)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if init_database:
            init_db()
        yield

    app = FastAPI(title="Pahunch API", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(DomainError)
    def domain_error_handler(_, error: DomainError) -> JSONResponse:
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/orders", response_model=list[Order])
    def list_orders(session: SessionDep) -> list[Order]:
        return list(session.exec(select(Order).order_by(Order.id)).all())

    @app.get("/api/orders/{order_id}", response_model=Order)
    def get_order(order_id: int, session: SessionDep) -> Order:
        order = session.get(Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        return order

    @app.get("/api/orders/{order_id}/slots", response_model=list[DeliverySlot])
    def get_slots(order_id: int, session: SessionDep) -> list[DeliverySlot]:
        if session.get(Order, order_id) is None:
            raise HTTPException(status_code=404, detail="Order not found")
        statement = select(DeliverySlot).where(DeliverySlot.order_id == order_id).order_by(DeliverySlot.start_at)
        return list(session.exec(statement).all())

    @app.post("/api/orders/{order_id}/validate-plan", response_model=PolicyVerdict)
    def validate_order_plan(
        order_id: int,
        request: PlanValidationRequest,
        session: SessionDep,
    ) -> PolicyVerdict:
        order = session.get(Order, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        slots = list(
            session.exec(select(DeliverySlot).where(DeliverySlot.order_id == order_id)).all()
        )
        return validate_plan(request.proposal, order, slots)

    @app.post("/api/orders/{order_id}/call", response_model=CallSession, status_code=201)
    def create_call(order_id: int, session: SessionDep) -> CallSession:
        return CallService(session).create_call(order_id)

    @app.get("/api/sessions/{session_id}", response_model=CallSession)
    def get_call_session(session_id: str, session: SessionDep) -> CallSession:
        return CallService(session).get_call(session_id)

    @app.post("/api/sessions/{session_id}/connect", response_model=CallSession)
    def connect_call(session_id: str, session: SessionDep) -> CallSession:
        return CallService(session).connect(session_id)

    @app.post("/api/sessions/{session_id}/context-confirmation", response_model=CallSession)
    def confirm_context(session_id: str, session: SessionDep) -> CallSession:
        return CallService(session).confirm_context(session_id)

    @app.post("/api/sessions/{session_id}/agent-turn", response_model=AgentTurnResponse)
    async def agent_turn(
        session_id: str,
        request: AgentTurnRequest,
        session: SessionDep,
    ) -> AgentTurnResponse:
        if planner_llm is None:
            raise DomainError(503, "LLM planner is not configured")
        planner = PlannerService(
            session,
            planner_llm,
            min_confidence=settings.planner_min_confidence,
            min_transcript_confidence=settings.planner_min_transcript_confidence,
        )
        return await planner.handle_turn(session_id, request)

    @app.post("/api/sessions/{session_id}/voice-turn", response_model=VoiceTurnResponse)
    async def voice_turn(
        session_id: str,
        session: SessionDep,
        audio: Annotated[
            UploadFile,
            File(description="One spoken turn: WAV, MP3, OGG, FLAC, AAC, or M4A"),
        ],
    ) -> VoiceTurnResponse:
        if speech is None:
            raise DomainError(503, "Gnani speech is not configured")
        if planner_llm is None:
            raise DomainError(503, "LLM planner is not configured")
        filename = audio.filename or ""
        content_type = audio.content_type or ""
        try:
            data = await audio.read(max_audio_bytes + 1)
        finally:
            await audio.close()
        service = VoiceTurnService(
            session,
            planner_llm,
            speech,
            max_audio_bytes=max_audio_bytes,
            min_confidence=settings.planner_min_confidence,
            min_transcript_confidence=settings.planner_min_transcript_confidence,
        )
        return await service.handle_audio(
            session_id,
            data,
            filename,
            content_type,
        )

    @app.post("/api/sessions/{session_id}/no-answer", response_model=CallSession)
    def no_answer(session_id: str, session: SessionDep) -> CallSession:
        return CallService(session).mark_no_answer(session_id)

    @app.post("/api/sessions/{session_id}/turns", response_model=TurnResult)
    def submit_turn(
        session_id: str,
        request: CustomerTurnRequest,
        session: SessionDep,
    ) -> TurnResult:
        return CallService(session).submit_turn(session_id, request)

    @app.post("/api/sessions/{session_id}/commit", response_model=CommitResult)
    def commit_plan(
        session_id: str,
        request: CommitRequest,
        session: SessionDep,
    ) -> CommitResult:
        return CallService(session).commit_plan(session_id, request.idempotency_key)

    @app.get("/api/sessions/{session_id}/courier-card", response_model=CourierCard)
    def courier_card(session_id: str, session: SessionDep) -> CourierCard:
        return CallService(session).get_courier_card(session_id)

    @app.get("/api/sessions/{session_id}/events", response_model=list[AuditEvent])
    def session_events(session_id: str, session: SessionDep) -> list[AuditEvent]:
        return CallService(session).list_events(session_id)

    @app.websocket("/ws/sessions/{session_id}/voice")
    async def voice_socket(websocket: WebSocket, session_id: str, session: SessionDep) -> None:
        await websocket.accept()
        if speech is None or planner_llm is None:
            await websocket.send_json(
                {"type": "error", "detail": "Voice is not configured on this server"}
            )
            await websocket.close(code=1013)
            return
        rt_config = realtime_config if realtime_config is not None else build_realtime_config(settings)
        await handle_voice_socket(
            websocket,
            session_id,
            session,
            planner_llm,
            speech,
            rt_config,
            streaming=new_streaming_stt(),
            max_audio_bytes=max_audio_bytes,
            min_confidence=settings.planner_min_confidence,
            min_transcript_confidence=settings.planner_min_transcript_confidence,
        )

    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


app = create_app()
