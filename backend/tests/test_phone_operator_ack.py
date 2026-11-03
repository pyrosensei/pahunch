import asyncio
import base64
import json

import pytest

from app.phone_media import PhoneSocket


@pytest.mark.parametrize("mode,expected", [("played", ["revision-1"]), ("cancelled", []), ("tts_failed", []), ("empty", [])])
def test_operator_revision_ack_requires_uncancelled_successful_playback_mark(mode, expected):
    class Socket:
        def __init__(self):
            self.events = [
                {"type": "websocket.receive", "text": json.dumps({"event": "mark", "streamSid": "stream", "mark": {"name": "pahunch-1"}})},
                {"type": "websocket.receive", "text": json.dumps({"event": "stop", "streamSid": "stream"})},
            ]
        async def send_json(self, value):
            pass
        async def receive(self):
            return self.events.pop(0)
        async def close(self, code=1000):
            pass

    async def run():
        acknowledgements = []
        async def turn(event):
            return False
        phone = PhoneSocket(Socket(), "stream", lambda *args: None, on_turn=turn, on_revision_ack=acknowledgements.append)
        await phone.receive()  # configuration
        await phone.receive()  # initial listen
        await phone.send_json({"type": "turn", "generation_id": 1, "planner": {"operator_revision_id": "revision-1"}})
        assert acknowledgements == []
        if mode != "empty":
            await phone.send_json({"type": "agent_audio_chunk", "generation_id": 1, "sample_rate": 8000, "chunk_base64": base64.b64encode(b'\0' * 320).decode()})
        await phone.send_json({"type": "agent_audio_end", "generation_id": 1, "audio_streamed": mode != "empty", "audio_error": "TTS_FAILED" if mode == "tts_failed" else None})
        if mode == "cancelled":
            await phone.send_json({"type": "stop_speaking", "generation_id": 1})
        await phone.receive()
        assert acknowledgements == expected
    asyncio.run(run())
