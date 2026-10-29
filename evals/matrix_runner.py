"""Live end-to-end evaluation matrix (T01-T22) for the Pahunch voice path.

Each scenario creates a call for an isolated fixture order, streams synthesized
Hinglish customer turns as PCM16 through /ws/sessions/{id}/voice (real Prisma
streaming STT), records the planner turns, policy verdicts, audits and commits,
and checks the scenario's expected outcome.

The server must run against the same disposable SQLite database:

    cd backend
    DATABASE_URL=sqlite:///./matrix.db uv run uvicorn app.main:app --port 8000

    cd backend
    DATABASE_URL=sqlite:///./matrix.db uv run python ../evals/matrix_runner.py

This runner deletes all rows in the configured database before it starts.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import random
import re
import sys
import time
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path

import httpx
import websockets
from sqlalchemy import delete
from sqlmodel import Session, SQLModel, create_engine, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings
from app.gnani import build_gnani_speech_client
from app.models import (
    AuditEvent,
    CallSession,
    DeliveryCommit,
    DeliverySlot,
    Order,
    OrderStatus,
    PaymentMode,
    PlanProposalRecord,
    PolicyDecisionRecord,
    Turn,
)

SCENARIOS_PATH = Path(__file__).with_name("matrix_scenarios.json")
REPORTS_DIR = Path(__file__).with_name("reports")
SAMPLE_RATE = 16_000
FRAME_MS = 100
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2
SUCCESS_CLAIM = re.compile(
    r"\b(done|completed|committed|successfully)\b|"
    r"\b(deliver\w*|reschedul\w*|confirm\w*|book\w*|update\w*)\s+(ho|kar)\s+(gaya|gayi|diya|di)\b",
    re.IGNORECASE,
)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 30},
)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------------------
# Fixture database


def reset_database() -> None:
    if not settings.database_url.startswith("sqlite"):
        raise SystemExit("The matrix runner expects a disposable SQLite database.")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        for model in (
            PolicyDecisionRecord,
            DeliveryCommit,
            PlanProposalRecord,
            Turn,
            AuditEvent,
            CallSession,
            DeliverySlot,
            Order,
        ):
            db.execute(delete(model))
        db.commit()


def insert_fixtures(scenarios: list[dict]) -> None:
    with Session(engine) as db:
        for scenario in scenarios:
            if scenario.get("mode") != "live":
                continue
            spec = scenario["order"]
            db.add(
                Order(
                    id=spec["id"],
                    merchant_name="SwiftCart",
                    delivery_address_summary=spec["delivery_address_summary"],
                    window_start=parse_time(spec["window_start"]),
                    window_end=parse_time(spec["window_end"]),
                    payment_mode=PaymentMode(spec["payment_mode"]),
                    requires_otp=spec.get("requires_otp", False),
                    declared_value_inr=spec.get("declared_value_inr", 0),
                    safe_drop_allowed=spec.get("safe_drop_allowed", False),
                    neighbour_drop_allowed=spec.get("neighbour_drop_allowed", False),
                    security_drop_allowed=spec.get("security_drop_allowed", False),
                    prior_attempts=spec.get("prior_attempts", 0),
                    status=OrderStatus.OUT_FOR_DELIVERY,
                )
            )
            for start, end in spec.get("slots", []):
                db.add(
                    DeliverySlot(
                        order_id=spec["id"],
                        start_at=parse_time(start),
                        end_at=parse_time(end),
                    )
                )
        db.commit()


def read_proposals(session_id: str) -> list[dict]:
    with Session(engine) as db:
        records = list(
            db.exec(
                select(PlanProposalRecord)
                .where(PlanProposalRecord.session_id == session_id)
                .order_by(PlanProposalRecord.created_at)
            ).all()
        )
        decisions = list(
            db.exec(
                select(PolicyDecisionRecord).where(
                    PolicyDecisionRecord.proposal_id.in_(
                        [record.id for record in records]
                    )
                )
            ).all()
        )
    by_proposal = {decision.proposal_id: decision for decision in decisions}
    output = []
    for record in records:
        decision = by_proposal.get(record.id)
        violations = json.loads(decision.violations_json) if decision else []
        output.append(
            {
                "id": record.id,
                "proposal": json.loads(record.proposal_json),
                "valid": decision.valid if decision else None,
                "violations": [item["code"] for item in violations],
                "allowed_alternatives": (
                    json.loads(decision.allowed_alternatives_json) if decision else []
                ),
            }
        )
    return output


def read_commits(session_id: str) -> list[dict]:
    with Session(engine) as db:
        records = list(
            db.exec(
                select(DeliveryCommit)
                .where(DeliveryCommit.session_id == session_id)
                .order_by(DeliveryCommit.committed_at)
            ).all()
        )
    return [
        {
            "id": record.id,
            "proposal_id": record.proposal_id,
            "idempotency_key": record.idempotency_key,
            "plan": json.loads(record.plan_json),
            "courier_card": json.loads(record.courier_card_json),
            "committed_at": record.committed_at.isoformat(),
        }
        for record in records
    ]


# ---------------------------------------------------------------------------
# Audio


def wav_to_pcm(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        if channels != 1 or width != 2:
            raise RuntimeError(f"Unexpected TTS format: {channels}ch {width * 8}bit")
        samples = wav.readframes(wav.getnframes())
    if rate == SAMPLE_RATE:
        return samples
    ratio = rate / SAMPLE_RATE
    count = int(len(samples) / 2 / ratio)
    out = bytearray(count * 2)
    for index in range(count):
        position = index * ratio
        left = int(position)
        fraction = position - left
        a = int.from_bytes(samples[left * 2 : left * 2 + 2], "little", signed=True)
        right = min(left + 1, len(samples) // 2 - 1)
        b = int.from_bytes(samples[right * 2 : right * 2 + 2], "little", signed=True)
        value = int(a + (b - a) * fraction)
        out[index * 2 : index * 2 + 2] = value.to_bytes(2, "little", signed=True)
    return bytes(out)


def add_noise(pcm: bytes, snr_db: float = 8.0) -> bytes:
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return pcm
    rms = math.sqrt(sum(value * value for value in samples) / len(samples)) or 1.0
    noise_rms = rms / (10 ** (snr_db / 20))
    rng = random.Random(20261105)
    noisy = array(
        "h",
        (
            max(-32768, min(32767, int(value + rng.gauss(0, noise_rms))))
            for value in samples
        ),
    )
    if sys.byteorder != "little":
        noisy.byteswap()
    return noisy.tobytes()


def pcm_frames(pcm: bytes) -> list[bytes]:
    padded = pcm + b"\x00" * (-len(pcm) % FRAME_BYTES)
    return [padded[i : i + FRAME_BYTES] for i in range(0, len(padded), FRAME_BYTES)]


class SpeechCache:
    def __init__(self, speech):
        self._speech = speech
        self._pcm: dict[str, bytes] = {}
        self.calls = 0
        self.characters = 0
        self.wav_bytes = 0

    async def pcm(self, text: str) -> bytes:
        if text not in self._pcm:
            audio = await self._speech.synthesize(text, container="wav")
            self.calls += 1
            self.characters += len(text)
            self.wav_bytes += len(audio.data)
            self._pcm[text] = wav_to_pcm(audio.data)
        return self._pcm[text]


# ---------------------------------------------------------------------------
# Voice socket


async def recv_json(ws, timeout: float) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout)
    if isinstance(raw, bytes):
        raise TypeError("Expected a JSON event, received binary audio")
    return json.loads(raw)


async def begin_listening(ws) -> None:
    await ws.send(json.dumps({"type": "start"}))
    deadline = time.perf_counter() + 15
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError("Server never confirmed listening")
        event = await recv_json(ws, remaining)
        if event["type"] == "listening":
            return
        if event["type"] == "error":
            raise RuntimeError(event["detail"])


async def stream_frames(ws, frames: list[bytes], *, start: bool) -> float:
    if start:
        await begin_listening(ws)
    for frame in frames:
        await ws.send(frame)
        await asyncio.sleep(FRAME_MS / 1000)
    await ws.send(json.dumps({"type": "stop"}))
    return time.perf_counter()


async def collect_turn(ws, timeout: float = 120) -> tuple[dict | None, list[dict], bool]:
    events: list[dict] = []
    turn = None
    turn_seen_at = None
    started = time.perf_counter()
    while True:
        now = time.perf_counter()
        budget = timeout - (now - started)
        if turn_seen_at is not None:
            budget = min(budget, 12 - (now - turn_seen_at))
        if budget <= 0:
            if turn is not None:
                return turn, events, False
            raise TimeoutError("No turn event arrived")
        try:
            event = await recv_json(ws, budget)
        except TimeoutError:
            if turn is not None:
                return turn, events, False
            raise
        events.append(event)
        if event["type"] == "error":
            raise RuntimeError(event["detail"])
        if event["type"] == "turn":
            turn = event
            turn_seen_at = time.perf_counter()
            if event.get("audio_error"):
                return turn, events, True
        elif event["type"] == "agent_audio":
            return turn, events, True


# ---------------------------------------------------------------------------
# Assertions


def check_turn(script: dict, result: dict) -> list[str]:
    failures: list[str] = []
    turn = result["turn_data"]
    verdict = turn.get("verdict")
    action = result["action"]
    message = result["message"]

    if "expect_action" in script and action != script["expect_action"]:
        failures.append(f"action {action} != {script['expect_action']}")
    if "expect_action_any" in script and action not in script["expect_action_any"]:
        failures.append(f"action {action} not in {script['expect_action_any']}")
    if "expect_state" in script and result["state"] != script["expect_state"]:
        failures.append(f"state {result['state']} != {script['expect_state']}")
    if (
        "expect_valid_if_present" in script
        and verdict is not None
        and verdict["valid"] != script["expect_valid_if_present"]
    ):
        failures.append(
            f"valid {verdict['valid']} != {script['expect_valid_if_present']}"
        )
    if "expect_violations" in script:
        codes = [item["code"] for item in (verdict or {}).get("violations", [])]
        for code in script["expect_violations"]:
            if code not in codes:
                failures.append(f"missing violation {code} (got {codes})")
    if "expect_allowed_alternatives_exclude" in script and verdict is not None:
        alternatives = verdict.get("allowed_alternatives", [])
        for action_name in script["expect_allowed_alternatives_exclude"]:
            if action_name in alternatives:
                failures.append(f"unsafe alternative offered: {action_name}")
    if (
        "expect_confirmed" in script
        and turn.get("customer_confirmed") != script["expect_confirmed"]
    ):
        failures.append(
            f"confirmed {turn.get('customer_confirmed')} "
            f"!= {script['expect_confirmed']}"
        )
    for needle in script.get("expect_message_contains", []):
        if needle.lower() not in message.lower():
            failures.append(f"message missing {needle!r}")
    any_words = script.get("expect_message_contains_any")
    if any_words and not any(word.lower() in message.lower() for word in any_words):
        failures.append(f"message missing any of {any_words}")
    if script.get("expect_message_no_otp_digits") and re.search(r"\d{4,}", message):
        failures.append("agent message contains a long digit sequence")
    if result["missing_audio"]:
        failures.append("agent audio missing")
    return failures


def check_final(scenario: dict, data: dict) -> list[str]:
    expected = scenario["expect"]
    failures: list[str] = []
    session = data["session"]
    commits = data["commits"]
    proposals = data["proposals"]

    if "final_state" in expected and session["state"] != expected["final_state"]:
        failures.append(f"final state {session['state']} != {expected['final_state']}")
    if "final_state_in" in expected and session["state"] not in expected["final_state_in"]:
        failures.append(
            f"final state {session['state']} not in {expected['final_state_in']}"
        )
    if len(commits) != expected.get("commits", 0):
        failures.append(f"commits {len(commits)} != {expected.get('commits', 0)}")
    if expected.get("exactly_one_commit") and len(commits) != 1:
        failures.append(f"expected exactly one commit, got {len(commits)}")
    if "proposal_count" in expected and len(proposals) != expected["proposal_count"]:
        failures.append(
            f"proposals {len(proposals)} != {expected['proposal_count']}"
        )

    committed = None
    if commits:
        committed = next(
            (item for item in proposals if item["id"] == commits[0]["proposal_id"]),
            proposals[-1] if proposals else None,
        )
    reference = committed or (proposals[-1] if proposals else None)
    if (
        reference is not None
        and "proposal_valid" in expected
        and reference["valid"] != expected["proposal_valid"]
    ):
        failures.append(
            f"proposal valid {reference['valid']} != {expected['proposal_valid']}"
        )
    if reference is not None and "primary_action" in expected:
        action = reference["proposal"]["primary"]["action"]
        if action != expected["primary_action"]:
            failures.append(f"primary action {action} != {expected['primary_action']}")
    if reference is not None and "proposal_fallback_action" in expected:
        fallback = reference["proposal"].get("fallback")
        action = fallback.get("action") if fallback else None
        if action != expected["proposal_fallback_action"]:
            failures.append(
                f"fallback action {action} != {expected['proposal_fallback_action']}"
            )
    if reference is not None and "proposal_fallback_delay_minutes" in expected:
        fallback = reference["proposal"].get("fallback") or {}
        delay = fallback.get("delay_minutes")
        if delay != expected["proposal_fallback_delay_minutes"]:
            failures.append(
                f"fallback delay {delay} != {expected['proposal_fallback_delay_minutes']}"
            )
    if "courier_action" in expected:
        card = data.get("courier_card")
        if card is None:
            failures.append("missing courier card")
        elif card["action"] != expected["courier_action"]:
            failures.append(
                f"courier action {card['action']} != {expected['courier_action']}"
            )
    if "courier_instructions_contain" in expected and data.get("courier_card"):
        lines = " | ".join(data["courier_card"]["instructions"]).lower()
        for needle in expected["courier_instructions_contain"]:
            if needle.lower() not in lines:
                failures.append(f"courier card missing {needle!r}")
    if "courier_window_start" in expected:
        card = data.get("courier_card")
        actual = card.get("window_start") if card else None
        if actual is None or datetime.fromisoformat(actual) != datetime.fromisoformat(
            expected["courier_window_start"]
        ):
            failures.append(
                f"courier window {actual} != {expected['courier_window_start']}"
            )
    if (
        "barge_in_seen" in expected
        and data.get("barge_in_seen") != expected["barge_in_seen"]
    ):
        failures.append(
            f"barge-in {data.get('barge_in_seen')} != {expected['barge_in_seen']}"
        )

    for check in data.get("checks", []):
        if not check["passed"]:
            detail = check["detail"]
            failures.append(f"{check['name']}: {detail}" if detail else check["name"])
    return failures


# ---------------------------------------------------------------------------
# Scenario execution


async def http_post(http: httpx.AsyncClient, path: str, payload: dict | None = None):
    if payload is None:
        return await http.post(path)
    return await http.post(path, json=payload)


async def commit(http: httpx.AsyncClient, session_id: str, key: str) -> dict:
    response = await http_post(
        http, f"/api/sessions/{session_id}/commit", {"idempotency_key": key}
    )
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = {"detail": response.text}
    return {"status": response.status_code, "body": body, "key": key}


async def run_scenario(http, speech: SpeechCache, scenario: dict) -> dict:
    order_spec = scenario["order"]
    order_id = order_spec["id"]
    result: dict = {
        "id": scenario["id"],
        "title": scenario["title"],
        "mode": scenario.get("mode", "live"),
        "passed": False,
        "failures": [],
        "turns": [],
        "commit_attempts": [],
        "checks": [],
    }

    created = await http_post(http, f"/api/orders/{order_id}/call")
    if created.status_code != 201:
        raise RuntimeError(f"create call failed: {created.status_code} {created.text}")
    session_id = created.json()["id"]
    result["session_id"] = session_id
    for path in ("connect", "context-confirmation"):
        response = await http_post(http, f"/api/sessions/{session_id}/{path}")
        if response.status_code != 200:
            raise RuntimeError(f"{path} failed: {response.status_code} {response.text}")

    ws_url = scenario["ws_base"].format(session_id=session_id)
    barge_in_seen = False
    result["audio_seconds"] = 0.0

    async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
        ready = await recv_json(ws, 20)
        if ready["type"] != "ready":
            raise RuntimeError(f"unexpected first event: {ready}")

        if scenario.get("silence_seconds"):
            await begin_listening(ws)
            silence = pcm_frames(b"\x00" * FRAME_BYTES * int(scenario["silence_seconds"] * 10))
            await stream_frames(ws, silence, start=False)
            try:
                event = await recv_json(ws, 2.5)
            except TimeoutError:
                result["checks"].append(
                    {"name": "silence produced no turn", "passed": True, "detail": ""}
                )
            else:
                result["checks"].append(
                    {
                        "name": "silence produced no turn",
                        "passed": False,
                        "detail": f"unexpected event after silence: {event['type']}",
                    }
                )
            result["audio_seconds"] += float(scenario["silence_seconds"])
            response = await http_post(http, f"/api/sessions/{session_id}/no-answer")
            if response.status_code != 200:
                raise RuntimeError(f"no-answer failed: {response.status_code} {response.text}")

        for index, script in enumerate(scenario.get("turns", [])):
            text = script["text"]
            pcm = await speech.pcm(text)
            if script.get("noise"):
                pcm = add_noise(pcm)
            frames = pcm_frames(pcm)

            stop_at = await stream_frames(ws, frames, start=not script.get("barge_in"))
            turn_event, events, complete = await collect_turn(ws)
            client_ms = round((time.perf_counter() - stop_at) * 1000)
            if turn_event is None:
                raise RuntimeError(f"turn {index + 1} produced no turn event")
            if any(event["type"] == "stop_speaking" for event in events):
                barge_in_seen = True
            result["audio_seconds"] += len(frames) * FRAME_MS / 1000
            agent_audio_bytes = sum(
                len(base64.b64decode(event["audio_base64"]))
                for event in events
                if event["type"] == "agent_audio"
            )
            planner = turn_event["planner"]
            turn_data = turn_event["turn"]
            verdict = turn_data.get("verdict")
            turn_result = {
                "index": index + 1,
                "input": text,
                "transcript": turn_event["transcript"],
                "action": planner.get("action"),
                "model": planner.get("model"),
                "planner": planner,
                "state": turn_event["state"],
                "verdict": (
                    {
                        "valid": verdict["valid"],
                        "violations": [
                            item["code"] for item in verdict.get("violations", [])
                        ],
                        "allowed_alternatives": verdict.get("allowed_alternatives", []),
                    }
                    if verdict
                    else None
                ),
                "confirmed": turn_data.get("customer_confirmed"),
                "message": turn_event["agent_message"],
                "timings": turn_event["timings"],
                "client_ms": client_ms,
                "agent_audio_bytes": agent_audio_bytes,
                "event_types": [event["type"] for event in events],
                "turn_data": turn_data,
                "missing_audio": not complete,
            }
            turn_result["failures"] = check_turn(script, turn_result)
            if SUCCESS_CLAIM.search(turn_event["agent_message"]):
                turn_result["failures"].append("agent message claims success prematurely")
            result["turns"].append(turn_result)

        await ws.close()

    result["barge_in_seen"] = barge_in_seen

    # Commit first: the session snapshot below must reflect the committed state.
    confirmed = [
        turn for turn in result["turns"] if turn["turn_data"].get("customer_confirmed")
    ]
    expected_commits = scenario["expect"].get("commits", 0)
    if confirmed and expected_commits:
        key = f"matrix-{scenario['id'].lower()}-{session_id[:8]}"
        first = await commit(http, session_id, key)
        result["commit_attempts"].append(first)
        if scenario["expect"].get("duplicate_commit"):
            replay = await commit(http, session_id, key)
            result["commit_attempts"].append(replay)
            other = await commit(http, session_id, f"{key}-other")
            result["commit_attempts"].append(other)
            same = (
                replay["status"] == 200
                and replay["body"].get("reused") is True
                and replay["body"].get("commit_id") == first["body"].get("commit_id")
            )
            result["checks"].append(
                {
                    "name": "idempotent replay",
                    "passed": same,
                    "detail": f"status {replay['status']} reused "
                    f"{replay['body'].get('reused')}",
                }
            )
            result["checks"].append(
                {
                    "name": "second key refused",
                    "passed": other["status"] == 409,
                    "detail": f"status {other['status']}",
                }
            )
    elif expected_commits == 0:
        refusal = await commit(
            http, session_id, f"matrix-{scenario['id'].lower()}-refused"
        )
        result["commit_attempts"].append(refusal)
        result["checks"].append(
            {
                "name": "unconfirmed commit refused",
                "passed": refusal["status"] == 409,
                "detail": f"status {refusal['status']}",
            }
        )

    session_response = await http.get(f"/api/sessions/{session_id}")
    session_response.raise_for_status()
    result["session"] = session_response.json()

    events: list[dict] = []
    deadline = time.perf_counter() + 3
    while True:
        # The server writes REALTIME_DISCONNECTED from its finally block, so
        # the first fetch after close() can race it.
        events_response = await http.get(f"/api/sessions/{session_id}/events")
        events_response.raise_for_status()
        events = events_response.json()
        if any(event["event_type"] == "REALTIME_DISCONNECTED" for event in events):
            break
        if time.perf_counter() >= deadline:
            break
        await asyncio.sleep(0.1)
    result["audit_events"] = [
        {
            "type": event["event_type"],
            "payload": json.loads(event["payload_json"]),
            "at": event["created_at"],
        }
        for event in events
    ]
    result["proposals"] = read_proposals(session_id)
    result["commits"] = read_commits(session_id)
    result["order"] = (await http.get(f"/api/orders/{order_id}")).json()

    if result["commits"]:
        card = await http.get(f"/api/sessions/{session_id}/courier-card")
        result["courier_card"] = card.json() if card.status_code == 200 else None
    else:
        result["courier_card"] = None

    if scenario["expect"].get("address_unchanged"):
        unchanged = (
            result["order"]["delivery_address_summary"]
            == order_spec["delivery_address_summary"]
        )
        result["checks"].append(
            {
                "name": "address unchanged",
                "passed": unchanged,
                "detail": result["order"]["delivery_address_summary"],
            }
        )

    # Committed plans must always reference a policy-valid proposal.
    for record in result["commits"]:
        proposal = next(
            (item for item in result["proposals"] if item["id"] == record["proposal_id"]),
            None,
        )
        valid = bool(proposal and proposal["valid"])
        result["checks"].append(
            {
                "name": "committed plan is policy-valid",
                "passed": valid,
                "detail": f"proposal {record['proposal_id']}",
            }
        )

    result["audit_event_types"] = [event["type"] for event in result["audit_events"]]
    for event_type in ("REALTIME_CONNECTED", "REALTIME_DISCONNECTED"):
        result["checks"].append(
            {
                "name": f"audit {event_type}",
                "passed": event_type in result["audit_event_types"],
                "detail": "",
            }
        )
    voice_turns = result["audit_event_types"].count("VOICE_TURN")
    result["checks"].append(
        {
            "name": "audit VOICE_TURN per processed turn",
            "passed": voice_turns == len(result["turns"]),
            "detail": f"{voice_turns} events, {len(result['turns'])} turns",
        }
    )

    result["failures"] = check_final(scenario, result)
    for turn in result["turns"]:
        for failure in turn["failures"]:
            result["failures"].append(f"turn {turn['index']}: {failure}")
    result["passed"] = not result["failures"]
    return result


# ---------------------------------------------------------------------------
# Reporting


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(float(ordered[int(position)]), 1)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 1
    )


def summarize(results: list[dict], usage: dict) -> dict:
    live = [item for item in results if item["mode"] == "live"]
    passed = [item for item in live if item["passed"]]
    turns = [turn for item in live for turn in item.get("turns", [])]
    total_ms = [turn["timings"]["total_ms"] for turn in turns]
    stt_ms = [turn["timings"]["stt_ms"] for turn in turns]
    planner_ms = [turn["timings"]["planner_ms"] for turn in turns]
    tts_ms = [turn["timings"]["tts_ms"] for turn in turns]
    client_ms = [turn["client_ms"] for turn in turns]
    commits = [commit for item in live for commit in item.get("commits", [])]
    violations_committed = sum(
        1
        for item in live
        for check in item["checks"]
        if check["name"] == "committed plan is policy-valid" and not check["passed"]
    )
    success_claims = sum(
        1
        for turn in turns
        if SUCCESS_CLAIM.search(turn["message"])
    )
    non_adversarial = {"T01", "T02", "T03", "T04", "T05", "T13", "T17", "T22"}
    non_adv_live = [item for item in live if item["id"] in non_adversarial]
    non_adv_completed = [
        item
        for item in non_adv_live
        if item["passed"] and len(item.get("commits", [])) == 1
    ]
    planner_errors: dict[str, int] = {}
    for turn in turns:
        planner = turn.get("planner") or {}
        code = planner.get("error_code")
        if planner.get("degraded") and code:
            planner_errors[code] = planner_errors.get(code, 0) + 1
    return {
        "live_scenarios": len(live),
        "deterministic_scenarios": len(results) - len(live),
        "passed": len(passed),
        "failed": len(live) - len(passed),
        "turns": len(turns),
        "commits": len(commits),
        "policy_violations_committed": violations_committed,
        "success_claims_before_commit": success_claims,
        "non_adversarial_completion": (
            round(len(non_adv_completed) / len(non_adv_live), 3) if non_adv_live else 0
        ),
        "latency_ms": {
            "turns": len(turns),
            "total_p50": percentile(total_ms, 0.5),
            "total_p95": percentile(total_ms, 0.95),
            "total_max": round(max(total_ms), 1) if total_ms else 0,
            "stt_p50": percentile(stt_ms, 0.5),
            "stt_p95": percentile(stt_ms, 0.95),
            "planner_p50": percentile(planner_ms, 0.5),
            "planner_p95": percentile(planner_ms, 0.95),
            "tts_p50": percentile(tts_ms, 0.5),
            "tts_p95": percentile(tts_ms, 0.95),
            "client_p50": percentile(client_ms, 0.5),
            "client_p95": percentile(client_ms, 0.95),
        },
        "planner_errors": planner_errors,
        "usage": usage,
        "failed_scenarios": [item["id"] for item in live if not item["passed"]],
    }


def markdown(report: dict) -> str:
    summary = report["summary"]
    latency = summary["latency_ms"]
    planner_errors = summary.get("planner_errors") or {}
    if planner_errors:
        breakdown = ", ".join(
            f"{code}: {count}" for code, count in sorted(planner_errors.items())
        )
        degraded = (
            f"- Degraded planner turns: {sum(planner_errors.values())} ({breakdown})"
        )
    else:
        degraded = "- Degraded planner turns: 0"
    lines = [
        f"# Pahunch live evaluation matrix — {report['generated_at']}",
        "",
        f"- Planner model: `{report['services']['planner']}`",
        f"- Speech: `{report['services']['stt']}` / `{report['services']['tts']}`",
        f"- Commit: {report['services']['commit']}",
        (f"- Live scenarios: {summary['live_scenarios']} "
        f"({summary['passed']} passed, {summary['failed']} failed)"),
        (f"- Deterministic-only scenarios: {summary['deterministic_scenarios']} "
        "(T19; no remote tool in the commit path)"),
        f"- Turns evaluated: {summary['turns']}; commits: {summary['commits']}",
        "",
        "## Acceptance checks",
        "",
        f"- Policy violations committed: {summary['policy_violations_committed']}",
        f"- Agent success claims before commit: {summary['success_claims_before_commit']}",
        (f"- Non-adversarial task completion: "
        f"{summary['non_adversarial_completion'] * 100:.0f}% "
        "(T01-T05, T13, T17, T22)"),
        (f"- Turn latency (server voice path): P50 {latency['total_p50']} ms, "
        f"P95 {latency['total_p95']} ms, max {latency['total_max']} ms "
        f"over {latency['turns']} turns; target was P95 < 2500 ms"),
        (f"- Client-observed stop-to-turn latency: P50 {latency['client_p50']} ms, "
        f"P95 {latency['client_p95']} ms"),
        (f"- Stage P50/P95: STT {latency['stt_p50']}/{latency['stt_p95']} ms, "
        f"planner {latency['planner_p50']}/{latency['planner_p95']} ms, "
        f"TTS {latency['tts_p50']}/{latency['tts_p95']} ms"),
        degraded,
        "",
        "## Scenarios",
        "",
        "| ID | Scenario | Result | Turns | Final state | Commits | Failures |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in report["scenarios"]:
        if item["mode"] != "live":
            lines.append(
                f"| {item['id']} | {item['title']} | deterministic-only | — | — | — | "
                f"{item.get('covers', '')[:80]} |"
            )
            continue
        failures = "; ".join(item["failures"]) if item["failures"] else ""
        lines.append(
            f"| {item['id']} | {item['title']} | "
            f"{'PASS' if item['passed'] else 'FAIL'} | {len(item.get('turns', []))} | "
            f"{item.get('session', {}).get('state', '—')} | "
            f"{len(item.get('commits', []))} | {failures} |"
        )

    lines += [
        "",
        "## Usage",
        "",
        (f"- Customer TTS synthesised: {summary['usage']['customer_tts_calls']} calls, "
        f"{summary['usage']['customer_wav_bytes']} WAV bytes"),
        f"- Agent audio returned: {summary['usage']['agent_audio_bytes']} bytes",
        (f"- Planner tokens: {summary['usage']['prompt_tokens']} prompt, "
        f"{summary['usage']['completion_tokens']} completion"),
        "",
        "Pricing is not included; token and audio counts are raw provider usage.",
        "",
        "## Method and honest limits",
        "",
        ("- Customer turns are scripted Hinglish text synthesised with Gnani Timbre, "
        "streamed as 16 kHz PCM16 over the real voice WebSocket. There is no human "
        "speaker, telephone codec, or carrier jitter in this run."),
        ("- Prisma streaming STT does not emit transcript confidence or partials. "
        "The live path relies on the explicit-confirmation gate before commit; "
        "low-confidence rejection is covered by deterministic tests "
        "(`LOW_TRANSCRIPT_CONFIDENCE`)."),
        ("- T19 is deterministic-only: this architecture commits through a local "
        "policy revalidation and SQLite write, not a remote tool. Failure paths "
        "(commit before confirmation, invalid revalidation) are covered by tests."),
        ("- Planner calls are bounded at 12 s. Provider stalls surface as "
        "`LLM_TIMEOUT` degraded turns, counted above; the turn falls back to a "
        "safe reply and the scenario fails its task check rather than hiding "
        "the stall."),
        "- Noise is white Gaussian noise mixed at 8 dB SNR, not recorded street audio.",
        ("- Each scenario uses a synthetic fixture order in a disposable database; "
        "the production cooldown and order lifecycle are exercised by unit tests."),
        "- Full audit trails are stored per scenario in the JSON report.",
        "",
    ]
    return "\n".join(lines)


def write_reports(report: dict) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = utc_stamp()
    json_path = REPORTS_DIR / f"matrix-{stamp}.json"
    latest_json = REPORTS_DIR / "matrix-latest.json"
    markdown_path = REPORTS_DIR / "matrix-latest.md"
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    json_path.write_text(payload, encoding="utf-8")
    latest_json.write_text(payload, encoding="utf-8")
    markdown_path.write_text(markdown(report), encoding="utf-8")
    return json_path, markdown_path


# ---------------------------------------------------------------------------
# Main


async def run(args) -> int:
    scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    if args.only:
        wanted = {item.strip().upper() for item in args.only.split(",")}
        scenarios = [item for item in scenarios if item["id"] in wanted]
        if not scenarios:
            raise SystemExit(f"No scenarios matched {args.only}")
    live = [item for item in scenarios if item.get("mode") == "live"]

    speech_client = build_gnani_speech_client(settings)
    if speech_client is None:
        raise SystemExit("Gnani speech is not configured; set GNANI_API_KEY in backend/.env.")
    if not settings.llm_model:
        raise SystemExit("LLM_MODEL is not configured.")
    speech = SpeechCache(speech_client)

    reset_database()
    insert_fixtures(live)

    ws_base = args.base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_base = f"{ws_base}/ws/sessions/{{session_id}}/voice"
    for scenario in live:
        scenario["ws_base"] = ws_base

    results: list[dict] = []
    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=httpx.Timeout(180)
    ) as http:
        health = await http.get("/health")
        if health.status_code != 200:
            raise SystemExit("API server is not healthy.")
        for scenario in scenarios:
            if scenario.get("mode") != "live":
                results.append(
                    {
                        "id": scenario["id"],
                        "title": scenario["title"],
                        "mode": "deterministic",
                        "covers": scenario.get("covers", ""),
                        "evidence": scenario.get("evidence", []),
                        "passed": True,
                    }
                )
                print(f"{scenario['id']} deterministic-only — {scenario.get('covers', '')}")
                continue
            print(f"{scenario['id']} running…", flush=True)
            started = time.perf_counter()
            try:
                result = await run_scenario(http, speech, scenario)
            except Exception as error:  # noqa: BLE001 - report, do not abort the matrix
                result = {
                    "id": scenario["id"],
                    "title": scenario["title"],
                    "mode": "live",
                    "passed": False,
                    "failures": [f"{type(error).__name__}: {error}"],
                    "turns": [],
                    "commit_attempts": [],
                    "checks": [],
                }
            result["duration_s"] = round(time.perf_counter() - started, 1)
            results.append(result)
            status = "PASS" if result["passed"] else "FAIL"
            message = "; ".join(result["failures"]) or "all checks passed"
            print(f"{scenario['id']} {status} ({result['duration_s']}s) — {message}")

    usage = {
        "customer_tts_calls": speech.calls,
        "customer_tts_characters": speech.characters,
        "customer_wav_bytes": speech.wav_bytes,
        "customer_audio_seconds": round(
            sum(
                item.get("audio_seconds", 0)
                for item in results
                if item["mode"] == "live"
            ),
            1,
        ),
        "agent_audio_bytes": sum(
            turn["agent_audio_bytes"]
            for item in results
            if item["mode"] == "live"
            for turn in item.get("turns", [])
        ),
        "prompt_tokens": sum(
            turn["planner"].get("prompt_tokens") or 0
            for item in results
            if item["mode"] == "live"
            for turn in item.get("turns", [])
        ),
        "completion_tokens": sum(
            turn["planner"].get("completion_tokens") or 0
            for item in results
            if item["mode"] == "live"
            for turn in item.get("turns", [])
        ),
    }
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "services": {
            "planner": settings.llm_model,
            "stt": f"{settings.gnani_stt_model} streaming ({settings.gnani_stt_language})",
            "tts": f"{settings.gnani_tts_model}/{settings.gnani_tts_voice}",
            "commit": "local policy revalidation + SQLite (no remote tool)",
        },
        "summary": summarize(results, usage),
        "scenarios": results,
    }
    json_path, markdown_path = write_reports(report)
    summary = report["summary"]
    print(
        f"\n{summary['passed']}/{summary['live_scenarios']} live scenarios passed; "
        f"turns={summary['turns']} P50={summary['latency_ms']['total_p50']}ms "
        f"P95={summary['latency_ms']['total_p95']}ms"
    )
    print(f"Report: {json_path}")
    print(f"Summary: {markdown_path}")
    return 0 if summary["failed"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--only", default="", help="Comma-separated scenario IDs")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
