"""Measure how Gnani applies its "one active request" limit to the configured keys.

Keys are read through the app settings (``backend/.env``) and are never printed:
every key is only ever referred to by position (key#1, key#2, ...). Each
experiment sends a few very short TTS requests, so the credit cost is small.

    cd backend
    .venv/Scripts/python.exe scripts/gnani_key_probe.py

Experiments
    A  two overlapping requests on the SAME key       -> is the limit really 1 per key?
    B  back-to-back requests on one key               -> does a finished request free its slot at once?
    C  overlapping requests on DIFFERENT keys         -> do extra keys add concurrency, or share one limit?
    D  realtime STT stream open + TTS on the same key -> does a live call's STT session use the slot?
    E  time-to-first-audio for the opening line       -> what the customer waits for at call start
    F  abandon a TTS stream early (what barge-in does) -> how long does the slot stay held afterwards?
    G  the app's own client at call start             -> what does queueing behind one slot cost the greeting?
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
from itertools import combinations
from time import perf_counter

import httpx

from app.config import settings
from app.gnani import build_gnani_speech_client, split_api_keys
from app.realtime import OPENING_LINE
from app.streaming_stt import GnaniStreamingSTTClient, StreamingSTTError
from app.voice import ACK_PHRASE

BASE = settings.gnani_base_url.rstrip("/")
SHORT_TEXT = "Ji."
SETTLE_SECONDS = 2.5
BROWSER_RATE = 48_000  # a typical AudioContext rate; the app asks Timbre for this rate


@dataclass
class Result:
    label: str
    status: int | str
    ms: int
    error: str | None = None

    @property
    def limited(self) -> bool:
        return self.status == 429

    def show(self) -> str:
        tail = f" {self.error}" if self.error else ""
        return f"{self.label}: {self.status} in {self.ms} ms{tail}"


def _ms(started: float) -> int:
    return round((perf_counter() - started) * 1000)


def _payload(text: str, sample_rate: int = 16_000) -> dict:
    return {
        "text": text,
        "voice": settings.gnani_tts_voice,
        "model": settings.gnani_tts_model,
        "language": settings.gnani_tts_language,
        "audio_config": {"container": "raw", "sample_rate": sample_rate},
    }


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        code = detail.get("error_code")
        return code if isinstance(code, str) else None
    return None


async def tts(client: httpx.AsyncClient, key: str, label: str, text: str = SHORT_TEXT) -> Result:
    started = perf_counter()
    try:
        response = await client.post(
            f"{BASE}/api/v1/tts/inference",
            headers={"X-API-Key-ID": key, "Content-Type": "application/json"},
            json=_payload(text),
        )
    except httpx.HTTPError as error:
        return Result(label, type(error).__name__, _ms(started))
    error_code = _error_code(response) if response.status_code != 200 else None
    return Result(label, response.status_code, _ms(started), error_code)


async def sse_first_audio(client: httpx.AsyncClient, key: str, text: str) -> str:
    started = perf_counter()
    first: int | None = None
    chunks = 0
    try:
        async with client.stream(
            "POST",
            f"{BASE}/api/v1/tts/sse",
            headers={"X-API-Key-ID": key, "Content-Type": "application/json"},
            json=_payload(text, BROWSER_RATE),
        ) as response:
            if response.status_code != 200:
                await response.aread()
                return f"HTTP {response.status_code} {_error_code(response) or ''}".strip()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if event.get("audio"):
                    chunks += 1
                    if first is None:
                        first = _ms(started)
    except httpx.HTTPError as error:
        return type(error).__name__
    return f"first audio {first} ms, whole stream {_ms(started)} ms, {chunks} chunks"


async def experiment_a(client: httpx.AsyncClient, key: str, rounds: int) -> list[Result]:
    print("\nA. two overlapping requests on key#1 (same key)")
    collected: list[Result] = []
    for index in range(rounds):
        pair = await asyncio.gather(
            tts(client, key, "req1"),
            tts(client, key, "req2"),
        )
        print(f"   round {index + 1}: " + " | ".join(item.show() for item in pair))
        collected += pair
        await asyncio.sleep(SETTLE_SECONDS)
    return collected


async def experiment_b(client: httpx.AsyncClient, key: str, count: int) -> list[Result]:
    print(f"\nB. {count} sequential requests on key#1, no pause between them")
    collected: list[Result] = []
    for index in range(count):
        result = await tts(client, key, f"seq{index + 1}")
        print(f"   {result.show()}")
        collected.append(result)
    await asyncio.sleep(SETTLE_SECONDS)
    return collected


async def experiment_c(
    client: httpx.AsyncClient, keys: list[str], rounds: int
) -> list[Result]:
    print("\nC. overlapping requests on DIFFERENT keys")
    collected: list[Result] = []
    for left, right in combinations(range(len(keys)), 2):
        for index in range(rounds):
            pair = await asyncio.gather(
                tts(client, keys[left], f"key#{left + 1}"),
                tts(client, keys[right], f"key#{right + 1}"),
            )
            print(f"   round {index + 1}: " + " | ".join(item.show() for item in pair))
            collected += pair
            await asyncio.sleep(SETTLE_SECONDS)
    return collected


async def experiment_d(client: httpx.AsyncClient, key: str) -> Result | None:
    print("\nD. realtime STT stream open on key#1, then TTS on key#1")
    stream = GnaniStreamingSTTClient(
        api_key=key,
        base_url=settings.gnani_base_url,
        sample_rate=settings.realtime_sample_rate,
        language=settings.gnani_stt_language,
        candidates=settings.streaming_stt_candidates,
        model=settings.gnani_stt_model,
    )
    try:
        await stream.connect()
    except StreamingSTTError as error:
        print(f"   STT stream could not open: {error}")
        return None
    try:
        result = await tts(client, key, "tts-while-stt-open")
        print(f"   {result.show()}")
        return result
    finally:
        await stream.close()
        await asyncio.sleep(SETTLE_SECONDS)


async def experiment_e(client: httpx.AsyncClient, key: str) -> None:
    print("\nE. opening line, streamed (what the customer waits for at call start)")
    print(f"   {await sse_first_audio(client, key, OPENING_LINE)}")
    await asyncio.sleep(SETTLE_SECONDS)


async def experiment_f(client: httpx.AsyncClient, key: str, rounds: int) -> list[int | None]:
    """Close a stream after its first chunk, then ask for the slot back."""
    print("\nF. abandon a TTS stream after its first chunk (barge-in), then request again at once")
    held: list[int | None] = []
    for index in range(rounds):
        try:
            async with client.stream(
                "POST",
                f"{BASE}/api/v1/tts/sse",
                headers={"X-API-Key-ID": key, "Content-Type": "application/json"},
                json=_payload(OPENING_LINE, BROWSER_RATE),
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    print(f"   round {index + 1}: stream refused, HTTP {response.status_code}")
                    held.append(None)
                    await asyncio.sleep(SETTLE_SECONDS)
                    continue
                async for line in response.aiter_lines():
                    if line.startswith("data:") and '"audio"' in line:
                        break
        except httpx.HTTPError as error:
            print(f"   round {index + 1}: {type(error).__name__}")
            held.append(None)
            continue

        abandoned = perf_counter()
        waited: int | None = None
        attempts = 0
        while perf_counter() - abandoned < 20:
            attempts += 1
            result = await tts(client, key, "after-abandon")
            if result.status == 200:
                waited = _ms(abandoned)
                break
            if not result.limited:
                print(f"   round {index + 1}: unexpected {result.show()}")
                break
            await asyncio.sleep(0.25)
        if waited is None:
            print(f"   round {index + 1}: slot still held after 20 s")
        else:
            print(
                f"   round {index + 1}: first request after abandoning succeeded "
                f"{waited} ms later ({attempts} attempt(s), "
                f"{attempts - 1} rate limited)"
            )
        held.append(waited)
        await asyncio.sleep(SETTLE_SECONDS)
    return held


async def _stream_ms(speech, text: str, rate: int) -> tuple[int, int]:
    started = perf_counter()
    first: int | None = None
    async for chunk in speech.stream_pcm(text, sample_rate=rate):
        if chunk and first is None:
            first = _ms(started)
    return (first if first is not None else -1), _ms(started)


async def _call_start(single_slot: bool, contended: bool) -> tuple[int, int]:
    """The greeting's first-audio time using the app's own client.

    ``contended`` reproduces what a socket does at open: a wasted acknowledgement
    synthesis at the default rate is already running when the greeting starts.
    """
    speech = build_gnani_speech_client(
        settings, extra_api_keys="" if single_slot else None
    )
    assert speech is not None
    try:
        ack_task = None
        if contended:
            ack_task = asyncio.create_task(
                speech.synthesize_pcm(ACK_PHRASE, sample_rate=settings.realtime_sample_rate)
            )
            await asyncio.sleep(0.03)  # the browser's config message arrives right after ready
        first, total = await _stream_ms(speech, OPENING_LINE, BROWSER_RATE)
        if ack_task is not None:
            await ack_task
        return first, total
    finally:
        await speech.aclose()
        await asyncio.sleep(SETTLE_SECONDS)


async def experiment_g(key_count: int, rounds: int) -> dict[str, list[int]]:
    print("\nG. app client at call start: greeting first-audio time, alone vs behind an ack synthesis")
    modes = [("1 slot", True)]
    if key_count >= 2:
        modes.append((f"{key_count} slots", False))
    out: dict[str, list[int]] = {}
    for name, single in modes:
        for contended in (False, True):
            label = f"{name}, {'ack running first' if contended else 'greeting alone'}"
            samples: list[int] = []
            for _ in range(rounds):
                try:
                    first, _ = await _call_start(single, contended)
                except Exception as error:  # a probe must report, not crash
                    print(f"   {label}: {type(error).__name__}")
                    continue
                samples.append(first)
            out[label] = samples
            if samples:
                print(f"   {label}: first audio {samples} ms (median {round(statistics.median(samples))} ms)")
    return out


def verdict(
    a: list[Result],
    b: list[Result],
    c: list[Result],
    d: Result | None,
    f: list[int | None],
    g: dict[str, list[int]],
    keys: int,
) -> None:
    print("\nReading of the results")
    limited_a = sum(1 for item in a if item.limited)
    print(f"  A: {limited_a} of {len(a)} overlapping requests were rate limited.")
    print(
        "     -> a second concurrent request on one key is refused."
        if limited_a
        else "     -> no 429 on overlap; this key allows more than one active request."
    )
    limited_b = sum(1 for item in b if item.limited)
    print(f"  B: {limited_b} of {len(b)} back-to-back requests were rate limited.")
    if keys >= 2:
        limited_c = sum(1 for item in c if item.limited)
        print(f"  C: {limited_c} of {len(c)} cross-key overlapping requests were rate limited.")
        if limited_c:
            print("     -> the keys share one limit. Extra keys add no concurrency.")
        else:
            print("     -> the keys are independent. Each extra key is a real extra slot.")
    else:
        print("  C: not run, only one key is configured. This is the experiment that answers")
        print("     whether extra keys help; add them to GNANI_API_KEYS and run again.")
    if d is not None:
        print(
            "  D: an open STT stream BLOCKS tts on the same key."
            if d.limited
            else "  D: an open STT stream does not block TTS on the same key."
        )
    waits = [item for item in f if item is not None]
    if waits:
        print(f"  F: slot free again {min(waits)}-{max(waits)} ms after an abandoned stream.")
    for label, samples in g.items():
        if samples:
            print(f"  G: {label}: median first audio {round(statistics.median(samples))} ms")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=3, help="repeats for the overlap tests")
    parser.add_argument("--sequential", type=int, default=6, help="requests for experiment B")
    parser.add_argument("--call-start-rounds", type=int, default=2, help="repeats for G")
    parser.add_argument("--skip-stt", action="store_true", help="skip experiment D")
    parser.add_argument("--skip-opening", action="store_true", help="skip experiments E, F and G")
    args = parser.parse_args()

    keys = split_api_keys(settings.gnani_api_key, settings.gnani_api_keys)
    if not keys:
        print("No Gnani key is configured in backend/.env.")
        return 1
    print(f"{len(keys)} distinct Gnani key(s) configured (values are never printed).")

    f: list[int | None] = []
    g: dict[str, list[int]] = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        # A key that cannot synthesise at all would make every later result meaningless.
        first = await tts(client, keys[0], "warm-up")
        print(f"warm-up on key#1: {first.show()}")
        if first.status != 200:
            print("Stopping: key#1 did not accept a plain TTS request.")
            return 1
        await asyncio.sleep(SETTLE_SECONDS)

        a = await experiment_a(client, keys[0], args.rounds)
        b = await experiment_b(client, keys[0], args.sequential)
        c = await experiment_c(client, keys, args.rounds) if len(keys) >= 2 else []
        d = None if args.skip_stt else await experiment_d(client, keys[0])
        if not args.skip_opening:
            await experiment_e(client, keys[0])
            f = await experiment_f(client, keys[0], 2)
    if not args.skip_opening:
        g = await experiment_g(len(keys), args.call_start_rounds)

    verdict(a, b, c, d, f, g, len(keys))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
