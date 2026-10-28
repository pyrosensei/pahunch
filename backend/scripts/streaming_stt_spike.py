"""Live spike for Prisma streaming STT — protocol verification round 2.

Verifies against the documented protocol:
  1. {"type": "commit"} closes an utterance and yields processing -> transcript.
  2. Multiple utterances work in one session (no reconnect needed).
  3. Server VAD closes a segment on trailing silence (continuous-mic mode).
  4. lang_code accepts a language list (hi-IN,en-IN) for code-mixed speech.

Run from backend/: uv run python scripts/streaming_stt_spike.py
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import time
import wave
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import settings
from app.gnani import build_gnani_speech_client

STREAM_URL = "wss://api.vachana.ai/stt/v3/stream"
RATE = 16_000
FRAME_BYTES = 1024  # 512 samples = 32 ms at 16 kHz
FRAME_SECONDS = FRAME_BYTES / 2 / RATE

UTTERANCES = [
    "आज शाम छह से आठ बजे आ जाइए, मैं घर पर रहूँगा।",
    "हाँ, ठीक है।",
]


def wav_to_pcm_frames(wav_bytes: bytes, frame_bytes: int = FRAME_BYTES) -> list[bytes]:
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
    frames = [samples[i : i + frame_bytes] for i in range(0, len(samples), frame_bytes)]
    if frames and len(frames[-1]) < frame_bytes:
        frames[-1] = frames[-1] + b"\x00" * (frame_bytes - len(frames[-1]))
    return frames


async def drain_until_transcript(ws, started_at, label: str) -> dict | None:
    processing_at = None
    while True:
        raw = await ws.recv()
        event = json.loads(raw)
        elapsed = round((time.perf_counter() - started_at) * 1000)
        kind = event.get("type")
        if kind == "transcript":
            engine_ms = (
                round((time.perf_counter() - processing_at) * 1000)
                if processing_at is not None
                else None
            )
            print(
                f"  [{label}] transcript at {elapsed} ms: text={event.get('text')!r} "
                f"lang={event.get('detected_language_code')} "
                f"engine_latency={event.get('latency')} engine_wall={engine_ms} ms "
                f"audio_duration_ms={event.get('audio_duration_ms')}",
                flush=True,
            )
            return event
        if kind in ("connected", "speech_start", "speech_end", "vad_start", "vad_end"):
            print(f"  [{label}] {kind} at {elapsed} ms", flush=True)
        elif kind == "processing":
            processing_at = time.perf_counter()
            print(f"  [{label}] processing at {elapsed} ms", flush=True)
        elif kind == "error":
            print(f"  [{label}] ERROR: {event}", flush=True)
            return None


async def main() -> None:
    speech = build_gnani_speech_client(settings)
    assert speech is not None and settings.gnani_api_key, "GNANI_API_KEY required"

    headers = {
        "x-api-key-id": settings.gnani_api_key,
        "x-model": "prisma-v3.0",
        "lang_code": "indic-auto",
        "x-language-candidates": "hi-IN,en-IN",
        "x-sample-rate": str(RATE),
        "x-api-request-id": "pahunch-spike-r2",
        "x-min-silence-ms": "5000",
    }
    async with websockets.connect(STREAM_URL, additional_headers=headers) as ws:
        first = json.loads(await ws.recv())
        print(f"first event: {json.dumps(first, ensure_ascii=False)}", flush=True)
        if first.get("type") != "connected":
            return

        wavs = [wav_to_pcm_frames((await speech.synthesize(t, container="wav")).data) for t in UTTERANCES]

        # 1. commit closes an utterance immediately
        print("--- test 1: commit control frame", flush=True)
        t0 = time.perf_counter()
        for frame in wavs[0]:
            await ws.send(frame)
            await asyncio.sleep(FRAME_SECONDS)
        await ws.send(json.dumps({"type": "commit"}))
        await drain_until_transcript(ws, t0, "commit")

        # 2. second utterance in the same session
        print("--- test 2: second utterance, same session", flush=True)
        t0 = time.perf_counter()
        for frame in wavs[1]:
            await ws.send(frame)
            await asyncio.sleep(FRAME_SECONDS)
        await ws.send(json.dumps({"type": "commit"}))
        await drain_until_transcript(ws, t0, "same-session")

        # 3. commit with no open utterance (cancel path) — does the session survive?
        print("--- test 3: commit with no open utterance", flush=True)
        t0 = time.perf_counter()
        await ws.send(json.dumps({"type": "commit"}))
        observed = None
        try:
            observed = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        except TimeoutError:
            print("  [empty-commit] no event within 8 s", flush=True)
        if observed is not None:
            print(
                f"  [empty-commit] event: type={observed.get('type')} "
                f"message={observed.get('message') or observed.get('text')}",
                flush=True,
            )

        # 4. session still usable after the empty commit
        print("--- test 4: utterance after empty commit", flush=True)
        t0 = time.perf_counter()
        for frame in wavs[1]:
            await ws.send(frame)
            await asyncio.sleep(FRAME_SECONDS)
        await ws.send(json.dumps({"type": "commit"}))
        await drain_until_transcript(ws, t0, "after-empty-commit")


if __name__ == "__main__":
    asyncio.run(main())
