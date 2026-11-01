import asyncio
import json
from datetime import UTC

import httpx
import pytest
from pydantic import ValidationError
from test_planner import db as planner_database
from test_planner import start_call, valid_proposal

from app.config import Settings
from app.errors import DomainError
from app.llm import (
    LLMGatewayError,
    LLMResponseError,
    LLMTimeoutError,
    OpenAICompatibleClient,
    build_llm_client,
    make_strict_json_schema,
)
from app.models import Order
from app.planner import PlannerService, _planner_schema
from app.schemas import AgentTurnRequest, PlannerDecision

db = planner_database


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


def test_gemini_uses_minimal_reasoning_and_keeps_complete_plan_schema():
    schema = _planner_schema()

    def handler(request):
        payload = json.loads(request.content)
        assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        assert payload["model"] == "gemini-3.1-flash-lite"
        assert payload["reasoning_effort"] == "minimal"
        assert "chat_template_kwargs" not in payload
        assert payload["response_format"]["json_schema"]["schema"] == schema
        assert payload["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "action": "RESPOND", "message": "Hello, how can I help?",
                "proposal": None, "escalation_reason": None, "confidence": 0.95,
            })}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        })

    async def run():
        client = OpenAICompatibleClient(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key="test-gemini-key", model="gemini-3.1-flash-lite", provider="gemini",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.complete_json(system_prompt="system", user_prompt="Hello", schema_name="planner_decision", schema=schema)
            assert PlannerDecision.model_validate_json(result.text).action.value == "RESPOND"
            assert result.completion_tokens == 30
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("provider,expected_key", [("nim", "test-nim-key"), ("gemini", "test-gemini-key")])
def test_builder_selects_only_the_chosen_providers_key(provider, expected_key):
    config = Settings(
        _env_file=None, llm_provider=provider, llm_api_key="test-nim-key",
        gemini_api_key="test-gemini-key", llm_model="gemini-3.1-flash-lite" if provider == "gemini" else "test-nim-model",
        llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai" if provider == "gemini" else "https://nim.example/v1",
    )
    client = build_llm_client(config)
    assert client is not None

    def handler(request):
        assert request.headers["authorization"] == f"Bearer {expected_key}"
        payload = json.loads(request.content)
        if provider == "gemini":
            assert payload["reasoning_effort"] == "minimal"
            assert "chat_template_kwargs" not in payload
        else:
            assert payload["chat_template_kwargs"] == {"enable_thinking": False}
            assert "reasoning_effort" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client._pool._transport = httpx.MockTransport(handler)

    async def run():
        try:
            await client.complete_json(system_prompt="system", user_prompt="user", schema_name="test", schema={"type": "object"})
        finally:
            await client.aclose()

    asyncio.run(run())


def test_missing_gemini_key_does_not_fall_back_to_nim_credentials():
    config = Settings(
        _env_file=None, llm_provider="gemini", gemini_api_key="", llm_api_key="test-nim-key",
        llm_base_url="https://generativelanguage.googleapis.com/v1beta/openai", llm_model="gemini-3.1-flash-lite",
    )
    assert build_llm_client(config) is None


@pytest.mark.parametrize("url", ["https://nim.example/v1", "http://generativelanguage.googleapis.com/v1beta/openai", "https://generativelanguage.googleapis.com.example/v1beta/openai"])
def test_gemini_key_cannot_be_sent_to_another_provider(url):
    config = Settings(
        _env_file=None, llm_provider="gemini", gemini_api_key="test-gemini-key",
        llm_base_url=url, llm_model="gemini-3.1-flash-lite",
    )
    with pytest.raises(ValueError, match="Gemini base URL"):
        build_llm_client(config)


@pytest.mark.parametrize("unsafe", [False, True])
def test_gemini_plan_still_requires_policy_and_explicit_confirmation(db, unsafe):
    requests = []
    proposal = valid_proposal()
    order = db.get(Order, 1043)
    proposal["primary"].update(
        window_start=order.window_start.replace(tzinfo=order.window_start.tzinfo or UTC).isoformat(),
        window_end=order.window_end.replace(tzinfo=order.window_end.tzinfo or UTC).isoformat(),
    )
    if unsafe:
        proposal["primary"]["action"] = "LEAVE_WITH_SECURITY"

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "action": "PROPOSE_PLAN", "message": "{plan_summary} {confirmation_question}",
            "proposal": proposal, "escalation_reason": None, "confidence": 0.95,
        })}}]})

    async def run():
        llm = OpenAICompatibleClient(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key="test-gemini-key", model="gemini-3.1-flash-lite", provider="gemini",
            transport=httpx.MockTransport(handler),
        )
        try:
            planner = PlannerService(db, llm)
            call = start_call(planner.calls, 1043)
            result = await planner.handle_turn(call.id, AgentTurnRequest(text="Leave it with security" if unsafe else "Please hand it to me"))
            assert result.turn.verdict.valid == (not unsafe)
            assert not result.turn.customer_confirmed
            with pytest.raises(DomainError):
                planner.calls.commit_plan(call.id, "gemini-before-confirmation")
            if not unsafe:
                confirmed = await planner.handle_turn(call.id, AgentTurnRequest(text="Yes please go ahead"))
                assert confirmed.turn.customer_confirmed
                committed = planner.calls.commit_plan(call.id, "gemini-confirmed-plan")
                assert committed.state == "COMMITTED"
                assert planner.calls.commit_plan(call.id, "gemini-confirmed-plan").reused
            assert len(requests) == 1
        finally:
            await llm.aclose()

    asyncio.run(run())


def test_gemini_busy_response_falls_back_without_retry_or_commit(db):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "synthetic busy response"}})

    async def run():
        llm = OpenAICompatibleClient(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key="test-gemini-key", model="gemini-3.1-flash-lite", provider="gemini",
            transport=httpx.MockTransport(handler),
        )
        try:
            planner = PlannerService(db, llm)
            call = start_call(planner.calls, 1043)
            result = await planner.handle_turn(call.id, AgentTurnRequest(text="Please hand it to me"))
            assert result.planner.error_code == "LLM_UNAVAILABLE"
            assert result.planner.wording_source == "template"
            assert not result.turn.customer_confirmed
            assert planner.calls.get_call(call.id).current_proposal_id is None
            assert len(requests) == 1
        finally:
            await llm.aclose()

    asyncio.run(run())
