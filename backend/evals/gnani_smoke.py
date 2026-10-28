import asyncio
import sys

from app.config import settings
from app.gnani import GnaniError, build_gnani_speech_client

SAMPLE = (
    "Theek hai. Direct handoff, paanch November shaam chhe se aath baje. "
    "Kya main plan aage badhaun?"
)


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    client = build_gnani_speech_client(settings)
    if client is None:
        print("Gnani smoke test skipped: set GNANI_API_KEY in .env.")
        return 2

    try:
        audio = await client.synthesize(SAMPLE)
        print(
            f"TTS ok: {len(audio.data)} bytes, {audio.content_type}, "
            f"{audio.latency_ms} ms"
        )
        transcript = await client.transcribe(
            audio.data,
            filename="smoke.wav",
            content_type=audio.content_type,
        )
        print(
            f"STT ok: {transcript.text!r}, language={transcript.language}, "
            f"confidence={transcript.confidence}, {transcript.latency_ms} ms"
        )
    except GnaniError as error:
        print(f"Gnani smoke test failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
