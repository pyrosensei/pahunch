import json
from collections import deque

import pytest

from app.gnani import SpeechAudio, Transcript
from app.llm import LLMCompletion


class ScriptedLLM:
    model_name = "scripted-planner-v1"

    def __init__(self, outcomes: list[object]):
        self._outcomes = deque(outcomes)
        self.requests: list[dict] = []

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: dict,
    ) -> LLMCompletion:
        self.requests.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "schema_name": schema_name,
                "schema": schema,
            }
        )
        if not self._outcomes:
            raise AssertionError("ScriptedLLM has no remaining outcome")
        outcome = self._outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, LLMCompletion):
            return outcome
        text = json.dumps(outcome) if isinstance(outcome, dict) else str(outcome)
        return LLMCompletion(
            text=text,
            model=self.model_name,
            latency_ms=12,
            prompt_tokens=240,
            completion_tokens=80,
        )


class ScriptedSpeech:
    def __init__(
        self,
        transcripts: list[object] | None = None,
        audios: list[object] | None = None,
    ):
        self._transcripts = deque(transcripts or [])
        self._audios = deque(audios or [])
        self.transcribe_calls: list[dict] = []
        self.synthesize_calls: list[str] = []

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str,
        content_type: str,
    ) -> Transcript:
        self.transcribe_calls.append(
            {
                "bytes": len(audio),
                "filename": filename,
                "content_type": content_type,
            }
        )
        if not self._transcripts:
            raise AssertionError("ScriptedSpeech has no remaining transcript")
        outcome = self._transcripts.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, Transcript)
        return outcome

    async def synthesize(self, text: str) -> SpeechAudio:
        self.synthesize_calls.append(text)
        if not self._audios:
            raise AssertionError("ScriptedSpeech has no remaining audio")
        outcome = self._audios.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, SpeechAudio)
        return outcome


@pytest.fixture
def scripted_llm():
    return ScriptedLLM


@pytest.fixture
def scripted_speech():
    return ScriptedSpeech
