from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

import websockets
from websockets.exceptions import ConnectionClosed

from app.config import Settings

FRAME_BYTES = 1024


class StreamingSTTError(Exception):
    pass


@dataclass(frozen=True)
class StreamingTranscript:
    text: str
    language: str | None = None
    latency_ms: int = 0


class StreamingSTTClient(Protocol):
    async def connect(self) -> None: ...

    async def begin_utterance(self) -> None: ...

    async def send_audio(self, pcm: bytes) -> None: ...

    async def cancel_utterance(self) -> None: ...

    async def finish_utterance(self) -> StreamingTranscript | None: ...

    async def close(self) -> None: ...


class GnaniStreamingSTTClient:
    """Prisma v3.0 realtime STT over WebSocket.

    Frames are forwarded as they arrive; ``finish_utterance`` sends the
    ``commit`` control frame, which closes the open utterance immediately
    instead of waiting for server-side silence detection.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.vachana.ai",
        sample_rate: int = 16_000,
        language: str = "indic-auto",
        candidates: str = "hi-IN,en-IN",
        model: str = "prisma-v3.0",
        transcript_timeout_seconds: float = 3.0,
    ):
        self._api_key = api_key
        self._sample_rate = sample_rate
        self._language = language
        self._candidates = candidates
        self._model = model
        self._transcript_timeout_seconds = transcript_timeout_seconds
        ws_scheme = "wss" if base_url.startswith("https") else "ws"
        host = (
            base_url.replace("https://", "")
            .replace("http://", "")
            .replace("wss://", "")
            .replace("ws://", "")
            .rstrip("/")
        )
        self._ws_url = f"{ws_scheme}://{host}/stt/v3/stream"
        self._ws: websockets.ClientConnection | None = None
        self._reader_task: asyncio.Task | None = None
        self._partial = bytearray()
        self._dirty = False
        self._queue: asyncio.Queue[tuple[str, str | None, float] | None] = asyncio.Queue()
        self._last_error: str | None = None

    async def connect(self) -> None:
        headers = {
            "x-api-key-id": self._api_key,
            "lang_code": self._language,
            "x-language-candidates": self._candidates,
            "x-sample-rate": str(self._sample_rate),
            "x-model": self._model,
            "x-api-request-id": "pahunch",
            # Local VAD endpoints at ~700ms of silence; keep the server from
            # closing segments mid-utterance so every transcript is a response
            # to our own commit.
            "x-min-silence-ms": "5000",
        }
        try:
            ws = await websockets.connect(self._ws_url, additional_headers=headers)
        except Exception as error:
            raise StreamingSTTError("Streaming STT connect failed") from error
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        except Exception as error:
            await ws.close()
            raise StreamingSTTError("Streaming STT handshake failed") from error
        if first.get("type") != "connected":
            detail = first.get("message") or first.get("detail") or "unknown error"
            await ws.close()
            raise StreamingSTTError(f"Streaming STT refused: {detail}")
        self._ws = ws
        self._reader_task = asyncio.create_task(self._reader())

    async def begin_utterance(self) -> None:
        """Discard segments from the previous utterance."""
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is None:
                await self._queue.put(None)
                return

    async def send_audio(self, pcm: bytes) -> None:
        ws = self._require_ws()
        self._partial.extend(pcm)
        try:
            while len(self._partial) >= FRAME_BYTES:
                frame = bytes(self._partial[:FRAME_BYTES])
                del self._partial[:FRAME_BYTES]
                await ws.send(frame)
                self._dirty = True
        except Exception as error:
            raise StreamingSTTError("Streaming STT send failed") from error

    async def cancel_utterance(self) -> None:
        """Close an aborted utterance and discard its transcript."""
        if not self._dirty:
            return
        self._dirty = False
        ws = self._require_ws()
        try:
            if self._partial:
                frame = bytes(self._partial)
                frame += b"\x00" * (FRAME_BYTES - len(frame))
                self._partial.clear()
                await ws.send(frame)
            await ws.send(json.dumps({"type": "commit"}))
        except Exception as error:
            raise StreamingSTTError("Streaming STT cancel failed") from error
        try:
            item = await asyncio.wait_for(self._queue.get(), timeout=1.0)
        except TimeoutError:
            return
        if item is None:
            raise StreamingSTTError(
                self._last_error or "Streaming STT session closed"
            )

    async def finish_utterance(self) -> StreamingTranscript | None:
        """Commit the open utterance and wait for the final transcript."""
        ws = self._require_ws()
        self._dirty = False
        try:
            if self._partial:
                frame = bytes(self._partial)
                frame += b"\x00" * (FRAME_BYTES - len(frame))
                self._partial.clear()
                await ws.send(frame)
            commit_sent = perf_counter()
            await ws.send(json.dumps({"type": "commit"}))
        except Exception as error:
            raise StreamingSTTError("Streaming STT commit failed") from error

        deadline = commit_sent + self._transcript_timeout_seconds
        texts: list[str] = []
        language: str | None = None
        latency_ms = 0
        while True:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                break
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except TimeoutError:
                break
            if item is None:
                if texts:
                    break
                raise StreamingSTTError(
                    self._last_error or "Streaming STT session closed"
                )
            text, detected_language, received_at = item
            texts.append(text)
            language = detected_language or language
            if received_at > commit_sent:
                latency_ms = round((received_at - commit_sent) * 1000)
                break
        if not texts:
            return None
        joined = texts[0]
        for next_text in texts[1:]:
            if next_text.startswith(joined):
                joined = next_text
            elif not joined.endswith(next_text):
                joined = f"{joined} {next_text}"
        return StreamingTranscript(text=joined, language=language, latency_ms=latency_ms)

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
        if ws is not None:
            try:
                await ws.close()
            except (ConnectionClosed, OSError):
                pass

    def _require_ws(self) -> websockets.ClientConnection:
        if self._ws is None:
            raise StreamingSTTError("Streaming STT is not connected")
        return self._ws

    async def _reader(self) -> None:
        try:
            async for raw in self._ws:
                event = json.loads(raw)
                if event.get("type") == "transcript":
                    text = event.get("text")
                    if isinstance(text, str) and text.strip():
                        await self._queue.put(
                            (text.strip(), event.get("detected_language_code"), perf_counter())
                        )
                elif event.get("type") == "error":
                    self._last_error = event.get("message") or "stream error"
                    break
        except (ConnectionClosed, ValueError) as error:
            self._last_error = self._last_error or str(error)
        finally:
            await self._queue.put(None)


def build_streaming_stt_client(settings: Settings) -> GnaniStreamingSTTClient | None:
    if not settings.gnani_api_key or not settings.streaming_stt_enabled:
        return None
    return GnaniStreamingSTTClient(
        api_key=settings.gnani_api_key,
        base_url=settings.gnani_base_url,
        sample_rate=settings.realtime_sample_rate,
        language=settings.gnani_stt_language,
        candidates=settings.streaming_stt_candidates,
        model=settings.gnani_stt_model,
        transcript_timeout_seconds=settings.streaming_stt_transcript_timeout_seconds,
    )
