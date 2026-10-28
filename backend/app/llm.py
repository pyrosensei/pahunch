from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel

from app.config import Settings


class LLMGatewayError(Exception):
    pass


class LLMTimeoutError(LLMGatewayError):
    pass


class LLMResponseError(LLMGatewayError):
    pass


@dataclass(frozen=True)
class LLMCompletion:
    text: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class StructuredLLM(Protocol):
    @property
    def model_name(self) -> str: ...

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: dict,
    ) -> LLMCompletion: ...


def make_strict_json_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()

    def make_objects_strict(node: object) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in node.values():
                make_objects_strict(value)
        elif isinstance(node, list):
            for value in node:
                make_objects_strict(value)

    make_objects_strict(schema)
    return schema


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 8.0,
        max_tokens: int = 320,
        temperature: float = 0.1,
        structured_mode: Literal["json_schema", "json_object"] = "json_schema",
        enable_thinking: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._structured_mode = structured_mode
        self._enable_thinking = enable_thinking
        self._transport = transport

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def chat_completions_url(self) -> str:
        if self._base_url.endswith("/chat/completions"):
            return self._base_url
        return f"{self._base_url}/chat/completions"

    def _response_format(self, schema_name: str, schema: dict) -> dict:
        if self._structured_mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            }
        return {"type": "json_object"}

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: dict,
    ) -> LLMCompletion:
        started = perf_counter()
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "response_format": self._response_format(schema_name, schema),
            # Without this, reasoning models consume the whole token budget in
            # reasoning_content and return content=None.
            "chat_template_kwargs": {"enable_thinking": self._enable_thinking},
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self.chat_completions_url,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as error:
            raise LLMTimeoutError("LLM provider timed out") from error
        except httpx.HTTPError as error:
            raise LLMGatewayError("LLM provider request failed") from error

        if response.status_code >= 400:
            raise LLMResponseError(f"LLM provider returned HTTP {response.status_code}")

        try:
            data = response.json()
            message = data["choices"][0]["message"]
            content = message["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMResponseError("LLM provider returned an unexpected response") from error
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("LLM provider returned empty structured output")

        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        response_model = data.get("model")
        return LLMCompletion(
            text=_strip_code_fence(content),
            model=response_model if isinstance(response_model, str) else self._model,
            latency_ms=round((perf_counter() - started) * 1_000),
            prompt_tokens=_optional_int(usage.get("prompt_tokens")),
            completion_tokens=_optional_int(usage.get("completion_tokens")),
        )


def build_llm_client(settings: Settings) -> OpenAICompatibleClient | None:
    if not (settings.llm_api_key and settings.llm_base_url and settings.llm_model):
        return None
    return OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        structured_mode=settings.llm_structured_mode,
        enable_thinking=settings.llm_enable_thinking,
    )
