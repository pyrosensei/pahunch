"""Measure endpointing on generated PCM only; no speech providers or microphone."""

import json
import math
import random
import struct

from app.realtime import RealtimeConfig, UtteranceBuffer

RATE = 16_000
FRAME_MS = 20


def frame(rms: int, *, noise: bool = False, index: int = 0) -> bytes:
    count = RATE * FRAME_MS // 1000
    if noise:
        rng = random.Random(index)
        values = [rng.gauss(0, rms) for _ in range(count)]
    else:
        values = [rms * math.sqrt(2) * math.sin(2 * math.pi * 200 * i / RATE) for i in range(count)]
    return struct.pack(f"<{count}h", *(round(max(-32768, min(32767, v))) for v in values))


def measure(label: str, noise_rms: int, speech_rms: int | None) -> dict:
    buffer = UtteranceBuffer(RealtimeConfig())
    starts = []
    ends = []
    for i in range(130):
        rms = speech_rms if speech_rms is not None and 50 <= i < 90 else noise_rms
        pcm = frame(rms, noise=rms == noise_rms, index=i)
        if buffer.add(pcm):
            starts.append((i + 1) * FRAME_MS)
        if buffer.complete:
            if buffer.has_utterance:
                ends.append((i + 1) * FRAME_MS)
            buffer.reset()
    return {
        "case": label,
        "onset_ms": next((t - 1000 for t in starts if t >= 1000), None),
        "endpoint_ms": next((t - 1800 for t in ends if t >= 1800), None),
        "false_onsets": len([t for t in starts if speech_rms is None or t < 1000]),
    }


def cap_measurement() -> dict:
    buffer = UtteranceBuffer(RealtimeConfig(max_utterance_ms=1000))
    for i in range(50):
        buffer.add(frame(20, noise=True, index=i))
    for i in range(150):
        buffer.add(frame(1600 if i % 15 < 10 else 20, index=i))
        if buffer.complete:
            return {
                "case": "intermittent_cap",
                "after_onset_ms": (i + 1) * FRAME_MS,
                "buffered_ms": len(buffer.pcm) * 1000 // (RATE * 2),
            }
    return {"case": "intermittent_cap", "after_onset_ms": None}


def main() -> None:
    results = [
        measure("quiet_speech", 20, 220),
        measure("normal_speech", 20, 1600),
        measure("speech_over_steady_noise", 800, 4000),
        measure("steady_noise_only", 800, None),
        cap_measurement(),
    ]
    print(
        json.dumps(
            {"source": "generated PCM, audio-sample time; no STT/TTS/LLM", "results": results},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
