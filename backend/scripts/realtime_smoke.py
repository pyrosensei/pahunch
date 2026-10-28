"""Live smoke test for the realtime voice WebSocket against real Gnani + NVIDIA.

Synthesizes each customer turn with Timbre, streams the audio as PCM16 frames
through /ws/sessions/{id}/voice, and prints the full event stream. Requires the
API server to be running: `uv run uvicorn app.main:app` from backend/, then
`uv run python scripts/realtime_smoke.py`.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import wave
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings
from app.gnani import build_gnani_speech_client

BASE = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000/ws/sessions/{session_id}/voice"
RATE = 16_000
FRAME_MS = 100
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2

CUSTOMER_TURNS = [
    "आज शाम छह से आठ बजे आ जाइए, मैं घर पर रहूँगा, ओटीपी रखूँगा।",
    "हाँ, ठीक है।",
]


def wav_to_pcm_frames(wav_bytes: bytes) -> list[bytes]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        rate = wav.getframerate()
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        samples = wav.readframes(wav.getnframes())
    if rate != RATE:
        ratio = rate / RATE
        count = int(len(samples) / 2 / ratio)
        out = bytearray(count * 2)
        for i in range(count):
            pos = i * ratio
            idx = int(pos)
            frac = pos - idx
            a = int.from_bytes(samples[idx * 2 : idx * 2 + 2], "little", signed=True)
            b_idx = min(idx + 1, len(samples) // 2 - 1)
            b = int.from_bytes(samples[b_idx * 2 : b_idx * 2 + 2], "little", signed=True)
            out[i * 2 : i * 2 + 2] = int(a + (b - a) * frac).to_bytes(
                2, "little", signed=True
            )
        samples = bytes(out)
    return [samples[i : i + FRAME_BYTES] for i in range(0, len(samples), FRAME_BYTES)]


def print_event(event: dict) -> None:
    kind = event["type"]
    if kind == "turn":
        verdict = event["turn"].get("verdict")
        verdict_text = ""
        if verdict:
            verdict_text = f"valid={verdict['valid']}"
            if verdict.get("violations"):
                verdict_text += " violations=" + ",".join(
                    v["code"] for v in verdict["violations"]
                )
        print(
            f"  turn: state={event['state']} transcript={event['transcript']!r} "
            f"action={event['planner']['action']} model={event['planner']['model']} "
            f"verdict[{verdict_text}] "
            f"stt={event['timings']['stt_ms']}ms planner={event['timings']['planner_ms']}ms "
            f"tts={event['timings']['tts_ms']}ms total={event['timings']['total_ms']}ms"
        )
    elif kind == "agent_audio":
        print(f"  agent_audio: {len(event['audio_base64'])} b64 chars")
    else:
        print(f"  {json.dumps(event, ensure_ascii=False)}")


async def main() -> None:
    speech = build_gnani_speech_client(settings)
    async with httpx.AsyncClient(base_url=BASE, timeout=httpx.Timeout(90)) as http:
        created = (await http.post("/api/orders/1042/call")).raise_for_status().json()
        session_id = created["id"]
        (await http.post(f"/api/sessions/{session_id}/connect")).raise_for_status()
        (await http.post(f"/api/sessions/{session_id}/context-confirmation")).raise_for_status()
        print(f"session {session_id} ready")

        async with websockets.connect(
            WS.format(session_id=session_id), max_size=16 * 1024 * 1024
        ) as ws:
            print_event(json.loads(await ws.recv()))  # ready
            async with asyncio.timeout(120):
                for text in CUSTOMER_TURNS:
                    await ws.send(json.dumps({"type": "start"}))
                    print_event(json.loads(await ws.recv()))  # listening
                    frames = wav_to_pcm_frames(
                        (await speech.synthesize(text, container="wav")).data
                    )
                    print(f"streaming {len(frames)} frames: {text}")
                    for frame in frames:
                        await ws.send(frame)
                        await asyncio.sleep(FRAME_MS / 1000)
                    await ws.send(json.dumps({"type": "stop"}))
                    confirmed = False
                    while True:
                        event = json.loads(await ws.recv())
                        print_event(event)
                        if event["type"] == "turn":
                            confirmed = bool(event["turn"].get("customer_confirmed"))
                        if event["type"] == "agent_audio":
                            break
                        if event["type"] == "error":
                            return
                    if confirmed:
                        commit = (
                            await http.post(
                                f"/api/sessions/{session_id}/commit",
                                json={"idempotency_key": f"smoke-{session_id}"},
                            )
                        ).raise_for_status()
                        print("COMMIT OK")
                        print(json.dumps(commit.json(), ensure_ascii=False, indent=2))
                        return
        print("done (no commit reached)")


if __name__ == "__main__":
    asyncio.run(main())
