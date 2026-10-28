import asyncio
import json

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from app.streaming_stt import GnaniStreamingSTTClient, StreamingSTTError


def run_scenario(handler, scenario, **client_kwargs):
    async def main():
        async with websockets.serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = GnaniStreamingSTTClient(
                api_key="test-key",
                base_url=f"ws://127.0.0.1:{port}",
                transcript_timeout_seconds=0.2,
                **client_kwargs,
            )
            result = await scenario(client)
            await client.close()
        return result

    return asyncio.run(main())


def connected_event() -> str:
    return json.dumps({"type": "connected", "message": "ready", "config": {}})


def test_connect_sends_handshake_headers():
    captured = {}

    async def handler(ws):
        captured["headers"] = dict(ws.request.headers)
        await ws.send(connected_event())
        await ws.wait_closed()

    async def scenario(client):
        await client.connect()

    run_scenario(handler, scenario)
    headers = captured["headers"]
    assert headers["x-api-key-id"] == "test-key"
    assert headers["lang_code"] == "indic-auto"
    assert headers["x-language-candidates"] == "hi-IN,en-IN"
    assert headers["x-model"] == "prisma-v3.0"
    assert headers["x-sample-rate"] == "16000"
    assert headers["x-min-silence-ms"] == "5000"
    assert headers["x-api-request-id"] == "pahunch"


def test_connect_rejects_non_connected_first_event():
    async def handler(ws):
        await ws.send(json.dumps({"type": "error", "message": "bad key"}))
        await ws.wait_closed()

    async def scenario(client):
        with pytest.raises(StreamingSTTError, match="bad key"):
            await client.connect()

    run_scenario(handler, scenario)


def test_audio_is_split_into_1024_byte_frames_and_flushed():
    received = []

    async def handler(ws):
        await ws.send(connected_event())
        while True:
            message = await ws.recv()
            if isinstance(message, bytes):
                received.append(message)
            else:
                break
        await ws.wait_closed()

    async def scenario(client):
        await client.connect()
        await client.send_audio(b"\x01" * 2500)
        return await client.finish_utterance()

    result = run_scenario(handler, scenario)
    assert result is None
    assert received == [
        b"\x01" * 1024,
        b"\x01" * 1024,
        b"\x01" * 452 + b"\x00" * 572,
    ]


def test_finish_utterance_returns_post_commit_transcript():
    async def handler(ws):
        await ws.send(connected_event())
        while True:
            message = await ws.recv()
            if isinstance(message, str):
                control = json.loads(message)
                if control.get("type") == "commit":
                    await ws.send(
                        json.dumps(
                            {
                                "type": "transcript",
                                "text": "आज शाम छह से आठ बजे",
                                "detected_language_code": "hi-IN",
                                "is_final": True,
                            }
                        )
                    )
                    await ws.wait_closed()
                    return

    async def scenario(client):
        await client.connect()
        await client.send_audio(b"\x00" * 1024)
        return await client.finish_utterance()

    transcript = run_scenario(handler, scenario)
    assert transcript.text == "आज शाम छह से आठ बजे"
    assert transcript.language == "hi-IN"
    assert 0 <= transcript.latency_ms < 2_000


def test_multiple_utterances_in_one_session():
    async def handler(ws):
        await ws.send(connected_event())
        commits = 0
        while True:
            message = await ws.recv()
            if isinstance(message, str):
                commits += 1
                await ws.send(
                    json.dumps({"type": "transcript", "text": f"utt {commits}"})
                )

    async def scenario(client):
        await client.connect()
        await client.send_audio(b"\x00" * 1024)
        first = await client.finish_utterance()
        await client.send_audio(b"\x00" * 1024)
        second = await client.finish_utterance()
        return first, second

    first, second = run_scenario(handler, scenario)
    assert first.text == "utt 1"
    assert second.text == "utt 2"


def test_session_error_event_raises_on_finish():
    async def handler(ws):
        await ws.send(connected_event())
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "message": "session died"}))

    async def scenario(client):
        await client.connect()
        await client.send_audio(b"\x00" * 1024)
        with pytest.raises(StreamingSTTError, match="session died"):
            await client.finish_utterance()

    run_scenario(handler, scenario)


def test_cancel_utterance_discards_the_transcript():
    async def handler(ws):
        await ws.send(connected_event())
        commits = 0
        while True:
            message = await ws.recv()
            if isinstance(message, str):
                commits += 1
                await ws.send(
                    json.dumps({"type": "transcript", "text": f"utt {commits}"})
                )

    async def scenario(client):
        await client.connect()
        await client.send_audio(b"\x00" * 1024)
        await client.cancel_utterance()
        await client.send_audio(b"\x00" * 1024)
        return await client.finish_utterance()

    transcript = run_scenario(handler, scenario)
    assert transcript.text == "utt 2"


def test_cancel_without_audio_sends_no_commit():
    captured = []

    async def handler(ws):
        await ws.send(connected_event())
        try:
            while True:
                message = await ws.recv()
                if isinstance(message, str):
                    captured.append(json.loads(message))
        except ConnectionClosed:
            pass

    async def scenario(client):
        await client.connect()
        await client.cancel_utterance()

    run_scenario(handler, scenario)
    assert captured == []


def test_begin_utterance_discards_stale_segments():
    async def handler(ws):
        await ws.send(connected_event())
        first_audio = True
        while True:
            message = await ws.recv()
            if isinstance(message, bytes) and first_audio:
                first_audio = False
                await ws.send(json.dumps({"type": "transcript", "text": "stale"}))
            elif isinstance(message, str):
                await ws.send(json.dumps({"type": "transcript", "text": "fresh"}))
                await ws.wait_closed()
                return

    async def scenario(client):
        await client.connect()
        await client.send_audio(b"\x00" * 1024)
        await asyncio.sleep(0.1)
        await client.begin_utterance()
        return await client.finish_utterance()

    transcript = run_scenario(handler, scenario)
    assert transcript.text == "fresh"
