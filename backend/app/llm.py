from __future__ import annotations

import json
import re
from dataclasses import dataclass
from time import perf_counter
from typing import ClassVar, Literal, Protocol

import httpx
from pydantic import BaseModel

from app.config import Settings
from app.http_pool import HttpClientPool

_PROVIDER_ERROR_TEXT_LIMIT = 2_000
_MISSING_PROPERTIES = re.compile(r"missing properties:\s*((?:'[^']+'\s*,?\s*)+)", re.IGNORECASE)
_UNQUOTED_MISSING_PROPERTIES = re.compile(
    r"missing properties:\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)",
    re.IGNORECASE,
)
_FIELD_NAME = re.compile(r"[^A-Za-z0-9_]")


class LLMGatewayError(Exception):
    def __init__(self, message, *, status_code=None, category="gateway"):
        super().__init__(message)
        self.status_code = status_code
        self.category = category


class LLMTimeoutError(LLMGatewayError):
    pass


class LLMResponseError(LLMGatewayError):
    pass


class LLMStructuredOutputError(LLMResponseError):
    """The provider rejected generated JSON against the requested strict schema."""

    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message, status_code=400, category="structured_output")
        self.detail = detail or "generated JSON failed provider schema validation"


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


def inline_schema_refs(schema: dict) -> dict:
    """Resolve $ref pointers so providers without $defs support accept the schema."""

    definitions = schema.get("$defs", {})

    def resolve(node: object, seen: frozenset[str] = frozenset()) -> object:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.split("/")[-1]
                target = definitions.get(name)
                if isinstance(target, dict) and name not in seen:
                    resolved = resolve(target, seen | {name})
                    extra = {key: value for key, value in node.items() if key != "$ref"}
                    if isinstance(resolved, dict):
                        return {**resolved, **extra}
                return {}
            return {
                key: resolve(value, seen)
                for key, value in node.items()
                if key != "$defs"
            }
        if isinstance(node, list):
            return [resolve(value, seen) for value in node]
        return node

    return resolve(schema)


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


def _structured_output_detail(message: object) -> str:
    fallback = "generated JSON failed provider schema validation"
    if not isinstance(message, str):
        return fallback
    text = message[:_PROVIDER_ERROR_TEXT_LIMIT]
    fields: list[str] = []
    quoted = _MISSING_PROPERTIES.search(text)
    for name in re.findall(r"'([^']+)'", quoted.group(1)) if quoted else []:
        field = _FIELD_NAME.sub("", name)
        if field and field not in fields:
            fields.append(field)
    if not fields:
        unquoted = _UNQUOTED_MISSING_PROPERTIES.search(text)
        for name in unquoted.group(1).split(",") if unquoted else []:
            field = _FIELD_NAME.sub("", name.strip())
            if field and field not in fields:
                fields.append(field)
    return f"missing required fields: {', '.join(fields[:10])}" if fields else fallback


def _provider_http_error(status_code: int, text: str) -> LLMGatewayError:
    data: object = None
    try:
        # Truncating first would turn a recoverable provider error into invalid JSON.
        data = json.loads(text)
    except ValueError:
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else error

    if status_code == 400 and code == "json_validate_failed":
        return LLMStructuredOutputError(
            "LLM provider rejected generated structured output",
            detail=_structured_output_detail(message),
        )
    if status_code in {401, 403}:
        category = "access"
    elif status_code == 402:
        category = "billing"
    elif status_code == 429:
        category = "rate_limit"
    elif status_code == 400:
        category = "invalid_request"
    elif status_code >= 500:
        category = "provider"
    else:
        category = "http"
    return LLMResponseError(
        f"LLM provider returned HTTP {status_code}",
        status_code=status_code,
        category=category,
    )


class OpenAICompatibleClient:
    """OpenAI-compatible JSON generation for Groq, OpenRouter and local Ollama."""

    ALLOWED_BASE_URLS: ClassVar[frozenset[str]] = frozenset({
        "https://api.groq.com/openai/v1",
        "https://api.groq.com/openai/v1/chat/completions",
        "https://openrouter.ai/api/v1",
        "https://ollama.com/v1",
        "http://127.0.0.1:11434/v1",
        "http://localhost:11434/v1",
    })

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 8.0,
        max_tokens: int = 1024,
        temperature: float = 0.1,
        structured_mode: Literal["json_schema", "json_object"] = "json_schema",
        provider: str = "groq",
        inline_refs: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        if self._base_url not in self.ALLOWED_BASE_URLS:
            raise ValueError("Provider base URL must be an approved provider or a local Ollama endpoint.")
        self._api_key = api_key
        self._model = model
        self._provider = provider
        self._inline_refs = inline_refs
        self._timeout_seconds = timeout_seconds
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._structured_mode = structured_mode
        self._transport = transport
        self._pool = HttpClientPool(timeout=timeout_seconds, transport=transport)

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
        if self._inline_refs:
            schema = inline_schema_refs(schema)
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self._temperature,
            "response_format": self._response_format(schema_name, schema),
        }
        if self._provider in {"groq", "ollama"}:
            # Reasoning controls exist on these endpoints; the others reject them.
            payload["max_completion_tokens"] = self._max_tokens
            payload["reasoning_effort"] = "low"
            payload["include_reasoning"] = False
        else:
            payload["max_tokens"] = self._max_tokens
        try:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            response = await self._pool.get().post(
                self.chat_completions_url,
                headers=headers,
                json=payload,
            )
        except httpx.TimeoutException as error:
            raise LLMTimeoutError("LLM provider timed out") from error
        except httpx.HTTPError as error:
            raise LLMGatewayError("LLM provider request failed", category="transport") from error

        if response.status_code >= 400:
            raise _provider_http_error(response.status_code, response.text)

        try:
            data = response.json()
            message = data["choices"][0]["message"]
            content = message["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise LLMResponseError("LLM provider returned an unexpected response", category="invalid_response") from error
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseError("LLM provider returned empty structured output", category="empty_output")

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

    async def aclose(self) -> None:
        await self._pool.aclose()


class GeminiClient:
    """Native Gemini JSON generation using the existing keep-alive HTTP pool."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 12.0,
        max_tokens: int = 1024,
        temperature: float = 1.0,
        thinking_level: Literal["minimal", "low", "medium", "high"] = "minimal",
        structured_mode: Literal["json_schema", "json_object"] = "json_schema",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        if self._base_url != "https://generativelanguage.googleapis.com/v1beta":
            raise ValueError("Gemini base URL must use the official Google HTTPS endpoint.")
        if re.fullmatch(r"gemini-[A-Za-z0-9._-]+", model) is None:
            raise ValueError("Gemini model must be a model code such as gemini-3.1-flash-lite.")
        self._api_key = api_key
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._thinking_level = thinking_level
        self._structured_mode = structured_mode
        self._pool = HttpClientPool(timeout=timeout_seconds, transport=transport)

    @property
    def model_name(self) -> str:
        return self._model

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        schema: dict,
    ) -> LLMCompletion:
        started = perf_counter()
        generation_config = {
            "temperature": self._temperature,
            "maxOutputTokens": self._max_tokens,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": self._thinking_level},
        }
        if self._structured_mode == "json_schema":
            # responseJsonSchema supports $defs/$ref. responseSchema uses a
            # different schema dialect and cannot carry this planner schema.
            generation_config["responseJsonSchema"] = schema
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": generation_config,
        }
        try:
            response = await self._pool.get().post(
                f"{self._base_url}/models/{self._model}:generateContent",
                headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                json=payload,
            )
        except httpx.TimeoutException as error:
            raise LLMTimeoutError("LLM provider timed out") from error
        except httpx.HTTPError as error:
            raise LLMGatewayError("LLM provider request failed", category="transport") from error

        if response.status_code >= 400:
            raise _provider_http_error(response.status_code, response.text)

        try:
            data = response.json()
            feedback = data.get("promptFeedback")
            if isinstance(feedback, dict) and feedback.get("blockReason"):
                raise LLMResponseError("Gemini blocked the prompt", category="blocked_output")
            candidate = data["candidates"][0]
            finish_reason = candidate.get("finishReason")
            if finish_reason == "MAX_TOKENS":
                raise LLMResponseError(
                    "Gemini output exceeded its token budget", category="truncated_output"
                )
            if finish_reason != "STOP":
                raise LLMResponseError("Gemini did not complete its response", category="blocked_output")
            parts = candidate["content"]["parts"]
            if not isinstance(parts, list):
                raise TypeError("Expected response parts")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            raise LLMResponseError(
                "Gemini returned an unexpected response", category="invalid_response"
            ) from error

        content = "".join(
            part["text"] for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
            and not part.get("thought", False)
        )
        if not content.strip():
            raise LLMResponseError("Gemini returned empty structured output", category="empty_output")

        usage = data.get("usageMetadata")
        usage = usage if isinstance(usage, dict) else {}
        output_tokens = _optional_int(usage.get("candidatesTokenCount"))
        thought_tokens = _optional_int(usage.get("thoughtsTokenCount"))
        completion_tokens = (
            (output_tokens or 0) + (thought_tokens or 0)
            if output_tokens is not None or thought_tokens is not None else None
        )
        model = data.get("modelVersion")
        return LLMCompletion(
            text=_strip_code_fence(content),
            model=model if isinstance(model, str) and model else self._model,
            latency_ms=round((perf_counter() - started) * 1_000),
            prompt_tokens=_optional_int(usage.get("promptTokenCount")),
            completion_tokens=completion_tokens,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


PROVIDER_DEFAULTS: ClassVar[dict[str, tuple[str, str]]] = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta", "gemini-3.1-flash-lite"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "openrouter": ("https://openrouter.ai/api/v1", "google/gemma-3-27b-it:free"),
    "ollama": ("https://ollama.com/v1", "gpt-oss:120b"),
}
# Ollama's structured output has no $defs support, so its schema is inlined.
_INLINE_REF_PROVIDERS = {"ollama"}


def build_llm_client(settings: Settings) -> GeminiClient | OpenAICompatibleClient | None:
    provider = settings.llm_provider
    default_base, default_model = PROVIDER_DEFAULTS.get(
        provider, PROVIDER_DEFAULTS["gemini"]
    )
    base_url = (settings.llm_base_url or default_base).rstrip("/")
    model = settings.llm_model or default_model
    if provider == "gemini":
        api_key = settings.gemini_api_key.strip()
        if not (api_key and base_url and model):
            return None
        return GeminiClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=settings.llm_timeout_seconds,
            max_tokens=settings.llm_max_tokens,
            temperature=settings.llm_temperature,
            thinking_level=settings.llm_thinking_level,
            structured_mode=settings.llm_structured_mode,
        )
    api_key = {
        "groq": settings.groq_api_key,
        "openrouter": settings.openrouter_api_key,
        "ollama": settings.ollama_api_key,
    }.get(provider, "").strip()
    if not api_key:
        return None
    return OpenAICompatibleClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=settings.llm_timeout_seconds,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        structured_mode=settings.llm_structured_mode,
        provider=provider,
        inline_refs=provider in _INLINE_REF_PROVIDERS,
    )
