"""Check that Gnani's speech APIs work in the format a phone line uses.

A phone call carries 8 kHz G.711 mu-law audio. This probe asks Timbre for exactly
that format, feeds it to Prisma's realtime stream at 8 kHz, and prints how Prisma
tags English, Hinglish and Hindi speech and when it closes each utterance.

It uses clean synthetic audio, so it proves the formats and the language tags. It
does NOT measure accuracy on a noisy handset. Keys come from backend/.env through
the app settings and are never printed.

    cd backend
    .venv/Scripts/python.exe scripts/telephony_audio_probe.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
import sys
from time import perf_counter

import httpx
import websockets

from app.config import settings
from app.gnani import split_api_keys

BASE = settings.gnani_base_url.rstrip("/")
WS_URL = "wss://" + BASE.removeprefix("https://").removeprefix("http://") + "/stt/v3/stream"

PCM_FRAME_BYTES = 1024  # Prisma wants exactly 1,024 bytes per frame
BYTES_PER_SECOND_8K = 16_000  # 8,000 samples/s * 2 bytes
FRAME_SECONDS = PCM_FRAME_BYTES / BYTES_PER_SECOND_8K  # 64 ms at 8 kHz

SAMPLES = (
    (
        "english",
        "en-IN",
        "Hello, my order is due today. Please hand it over to me between six and eight in the evening.",
    ),
    (
        "hinglish",
        "hi-en",
        "Mera order aaj aa raha hai, please usse shaam ko chhe se aath baje ke beech mujhe hi dena.",
    ),
    (
        "hindi",
        "hi-IN",
        "मेरा ऑर्डर आज आ रहा है, कृपया इसे शाम छह से आठ बजे के बीच मुझे ही देना।",
    ),
)
FORMATS = ("verbatim", "codemix")


def _ulaw_table() -> list[int]:
    table = []
    for value in range(256):
        u = ~value & 0xFF
        t = (((u & 0x0F) << 3) + 0x84) << ((u & 0x70) >> 4)
        table.append((0x84 - t) if (u & 0x80) else (t - 0x84))
    return table


_ULAW = _ulaw_table()


def ulaw_to_pcm16(data: bytes) -> bytes:
    return struct.pack(f"<{len(data)}h", *(_ULAW[byte] for byte in data))


def pcm_stats(pcm: bytes) -> tuple[int, float]:
    count = len(pcm) // 2
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / count)
    return peak, rms


def _ms(started: float) -> int:
    return round((perf_counter() - started) * 1000)


async def synth_mulaw(
    client: httpx.AsyncClient, key: str, text: str, language: str
) -> tuple[bytes, int, int, list[int]]:
    payload = {
        "text": text,
        "voice": settings.gnani_tts_voice,
        "model": settings.gnani_tts_model,
        "language": language,
        "audio_config": {"container": "mulaw"},
    }
    started = perf_counter()
    first: int | None = None
    sizes: list[int] = []
    parts: list[bytes] = []
    async with client.stream(
        "POST",
        f"{BASE}/api/v1/tts/sse",
        headers={"X-API-Key-ID": key, "Content-Type": "application/json"},
        json=payload,
    ) as response:
        if response.status_code != 200:
            await response.aread()
            raise RuntimeError(f"TTS answered HTTP {response.status_code}")
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            audio = event.get("audio")
            if isinstance(audio, str) and audio:
                chunk = base64.b64decode(audio)
                if first is None:
                    first = _ms(started)
                sizes.append(len(chunk))
                parts.append(chunk)
    return b"".join(parts), (first if first is not None else -1), _ms(started), sizes


async def run_stream(key: str, pcm8k: bytes, fmt: str, tail_seconds: float = 2.5) -> dict:
    """Send paced audio, then silence, and record when Prisma reacts. No commit is sent."""
    # The older header spellings are the ones the live service honours together with
    # a language list; the app uses them too (see streaming_stt.py).
    headers = {
        "x-api-key-id": key,
        "x-model": "prisma-v3.0",
        "lang_code": "indic-auto",
        "x-language-candidates": "hi-IN,en-IN",
        "x-sample-rate": "8000",
        "x-format": fmt,
        "x-min-silence-ms": "700",
        "x-idle-timeout-sec": "60",
    }
    speech_seconds = len(pcm8k) / BYTES_PER_SECOND_8K
    events: list[tuple[str, float, dict]] = []
    ws = await websockets.connect(WS_URL, additional_headers=headers)
    try:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        if first.get("type") != "connected":
            return {"error": f"refused: {first.get('message') or first.get('detail')}"}
        started = perf_counter()

        async def reader() -> None:
            async for raw in ws:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                events.append((str(event.get("type")), perf_counter() - started, event))
                if event.get("type") == "transcript":
                    return

        read_task = asyncio.create_task(reader())
        padded = pcm8k + b"\x00" * int(tail_seconds * BYTES_PER_SECOND_8K)
        padded += b"\x00" * (-len(padded) % PCM_FRAME_BYTES)
        next_send = perf_counter()
        for offset in range(0, len(padded), PCM_FRAME_BYTES):
            await ws.send(padded[offset : offset + PCM_FRAME_BYTES])
            next_send += FRAME_SECONDS
            await asyncio.sleep(max(0.0, next_send - perf_counter()))
            if read_task.done():
                break
        try:
            await asyncio.wait_for(read_task, timeout=4)
        except TimeoutError:
            read_task.cancel()
    finally:
        await ws.close()

    result: dict = {"speech_seconds": round(speech_seconds, 2), "events": []}
    for kind, at, event in events:
        entry = {"type": kind, "at_s": round(at, 2)}
        if kind == "transcript":
            entry["text"] = event.get("text")
            entry["language"] = event.get("detected_language_code")
            entry["after_speech_end_s"] = round(at - speech_seconds, 2)
            entry["stt_latency_ms"] = event.get("latency")
        elif kind == "error":
            entry["message"] = event.get("message")
        result["events"].append(entry)
    return result


async def main() -> int:
    keys = split_api_keys(settings.gnani_api_key, settings.gnani_api_keys)
    if not keys:
        print("No Gnani key is configured in backend/.env.")
        return 1
    key = keys[0]
    print("Phone-format check with clean synthetic audio (keys are never printed).")

    async with httpx.AsyncClient(timeout=30.0) as client:
        audio: dict[str, bytes] = {}
        print("\nTTS as 8 kHz G.711 mu-law (container=mulaw), the exact payload a phone line carries")
        for label, language, text in SAMPLES:
            try:
                data, first_ms, total_ms, sizes = await synth_mulaw(client, key, text, language)
            except Exception as error:  # report, never crash the probe
                print(f"  {label:9s} language={language:6s} FAILED: {type(error).__name__}: {error}")
                continue
            pcm = ulaw_to_pcm16(data)
            peak, rms = pcm_stats(pcm)
            header = "WAV header present!" if data[:4] == b"RIFF" else "raw, no header"
            print(
                f"  {label:9s} language={language:6s} first audio {first_ms} ms, "
                f"{len(sizes)} chunks, {len(data)} bytes = {len(data) / 8000:.1f} s of speech, {header}, "
                f"decoded peak {peak} rms {rms:.0f}"
            )
            audio[label] = pcm
            await asyncio.sleep(2.5)

    print("\nPrisma realtime at 8 kHz, its own voice-activity detection closing each utterance (no commit sent)")
    for label, _, text in SAMPLES:
        pcm = audio.get(label)
        if pcm is None:
            continue
        print(f"\n  [{label}] said: {text}")
        for fmt in FORMATS:
            try:
                outcome = await run_stream(key, pcm, fmt)
            except Exception as error:
                print(f"    format={fmt:8s} FAILED: {type(error).__name__}: {error}")
                continue
            if "error" in outcome:
                print(f"    format={fmt:8s} {outcome['error']}")
                continue
            line = [f"speech {outcome['speech_seconds']} s"]
            transcript = None
            for event in outcome["events"]:
                if event["type"] == "transcript":
                    transcript = event
                elif event["type"] in {"speech_start", "processing"}:
                    line.append(f"{event['type']}@{event['at_s']}s")
            print(f"    format={fmt:8s} " + " ".join(line))
            if transcript is None:
                print("      no transcript arrived")
            else:
                print(
                    f"      transcript@{transcript['at_s']}s "
                    f"(+{transcript['after_speech_end_s']} s after speech ended, "
                    f"stt {transcript['stt_latency_ms']} ms) "
                    f"language={transcript['language']} text={transcript['text']!r}"
                )
            await asyncio.sleep(1.0)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(asyncio.run(main()))
