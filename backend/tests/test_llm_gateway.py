import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.llm import (
    LLMGatewayError,
    LLMResponseError,
    LLMTimeoutError,
    OpenAICompatibleClient,
    make_strict_json_schema,
)
from app.schemas import PlannerDecision


def make_client(handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="https://llm.example/v1",
        api_key="test-key",
        model="test-model",
        timeout_seconds=2,
        transport=httpx.MockTransport(handler),
    )


def test_openai_compatible_request_and_response_parsing():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "served-model-v2",
                "choices": [
                    {
                        "message": {
                            "content": "```json\n{\"ok\": true}\n```",
                        }
                    }
                ],
                "usage": {"prompt_tokens": 101, "completion_tokens": 22},
            },
        )

    client = make_client(handler)
    completion = asyncio.run(
        client.complete_json(
            system_prompt="system",
            user_prompt="user",
            schema_name="planner_decision",
            schema={"type": "object"},
        )
    )

    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    payload = captured["payload"]
    assert payload["model"] == "test-model"
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert completion.text == '{"ok": true}'
    assert completion.model == "served-model-v2"
    assert completion.prompt_tokens == 101
    assert completion.completion_tokens == 22
    assert completion.latency_ms >= 0


def test_json_object_mode_does_not_send_a_strict_schema():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "{}"}}]},
        )

    client = OpenAICompatibleClient(
        base_url="https://llm.example/v1/chat/completions",
        api_key="test-key",
        model="test-model",
        structured_mode="json_object",
        transport=httpx.MockTransport(handler),
    )
    completion = asyncio.run(
        client.complete_json(
            system_prompt="system",
            user_prompt="user",
            schema_name="planner_decision",
            schema={"type": "object"},
        )
    )
    assert completion.text == "{}"


def test_timeout_is_mapped_to_gateway_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow provider", request=request)

    client = make_client(handler)
    with pytest.raises(LLMTimeoutError):
        asyncio.run(
            client.complete_json(
                system_prompt="system",
                user_prompt="user",
                schema_name="planner_decision",
                schema={"type": "object"},
            )
        )


def test_http_and_malformed_provider_errors_are_mapped():
    def http_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = make_client(http_error)
    with pytest.raises(LLMResponseError, match="429"):
        asyncio.run(
            client.complete_json(
                system_prompt="system",
                user_prompt="user",
                schema_name="planner_decision",
                schema={"type": "object"},
            )
        )

    def malformed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    client = make_client(malformed)
    with pytest.raises(LLMGatewayError, match="unexpected response"):
        asyncio.run(
            client.complete_json(
                system_prompt="system",
                user_prompt="user",
                schema_name="planner_decision",
                schema={"type": "object"},
            )
        )


def test_strict_schema_requires_every_object_property():
    schema = make_strict_json_schema(PlannerDecision)

    def walk(node):
        if isinstance(node, dict):
            assert "default" not in node
            properties = node.get("properties")
            if isinstance(properties, dict):
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(properties)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    assert set(schema["required"]) == set(schema["properties"])


def test_planner_decision_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "action": "ASK_CLARIFICATION",
                "message": "Kaunsa time theek hai?",
                "proposal": None,
                "escalation_reason": None,
                "confidence": 0.8,
                "commit_plan": True,
            }
        )
