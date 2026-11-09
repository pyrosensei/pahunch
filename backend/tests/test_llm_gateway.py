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
    GeminiClient,
    LLMGatewayError,
    LLMResponseError,
    LLMStructuredOutputError,
    LLMTimeoutError,
    OpenAICompatibleClient,
    build_llm_client,
    make_strict_json_schema,
)
from app.models import Order
from app.planner import PLANNER_OUTPUT_SHAPE, PlannerService, _planner_schema
from app.schemas import AgentTurnRequest, PlannerDecision

db = planner_database


def make_client(handler) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="https://api.groq.com/openai/v1",
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

    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    payload = captured["payload"]
    assert payload["model"] == "test-model"
    assert "chat_template_kwargs" not in payload
    assert payload["reasoning_effort"] == "low"
    assert payload["include_reasoning"] is False
    assert payload["max_completion_tokens"] == 1024
    assert "max_tokens" not in payload
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
        base_url="https://api.groq.com/openai/v1/chat/completions",
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


@pytest.mark.parametrize(
    ("status", "code", "expected_type", "expected_category"),
    [
        (400, "json_validate_failed", LLMStructuredOutputError, "structured_output"),
        (400, "invalid_request_error", LLMResponseError, "invalid_request"),
        (401, "authentication_error", LLMResponseError, "access"),
        (403, "permission_error", LLMResponseError, "access"),
        (429, "rate_limit_exceeded", LLMResponseError, "rate_limit"),
        (503, "service_unavailable", LLMResponseError, "provider"),
    ],
)
def test_provider_http_failures_are_classified(status, code, expected_type, expected_category):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "synthetic failure"}})

    client = make_client(handler)
    with pytest.raises(expected_type) as raised:
        asyncio.run(
            client.complete_json(
                system_prompt="system",
                user_prompt="user",
                schema_name="planner_decision",
                schema={"type": "object"},
            )
        )
    assert raised.value.status_code == status
    assert raised.value.category == expected_category
    if expected_type is not LLMStructuredOutputError:
        assert not isinstance(raised.value, LLMStructuredOutputError)


def test_json_validate_failed_detail_is_bounded_and_sanitized():
    raw = (
        "Generated JSON does not match the expected schema. See 'failed_generation' for details. "
        "Error: jsonschema: '' does not validate with /required: missing properties: "
        "'awaiting_continuation', 'request_details', 'missing_fields'. "
        "Untrusted diagnostic: <script>alert('x')</script>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "json_validate_failed", "message": raw}})

    client = make_client(handler)
    with pytest.raises(LLMStructuredOutputError) as raised:
        asyncio.run(
            client.complete_json(
                system_prompt="system",
                user_prompt="user",
                schema_name="planner_decision",
                schema={"type": "object"},
            )
        )
    detail = raised.value.detail
    assert detail == "missing required fields: awaiting_continuation, request_details, missing_fields"
    assert "failed_generation" not in detail
    assert "script" not in detail
    assert len(detail) <= 300


def test_large_json_validate_failed_body_keeps_the_repair_category():
    payload = {
        "error": {
            "code": "json_validate_failed",
            "message": (
                "Error: jsonschema: '' does not validate with /required: missing properties: "
                "'awaiting_continuation', 'request_details'. " + "diagnostic padding " * 300
            ),
            "failed_generation": "untrusted generation " * 300,
        }
    }
    assert len(json.dumps(payload)) > 2_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=payload)

    client = make_client(handler)
    with pytest.raises(LLMStructuredOutputError) as raised:
        asyncio.run(
            client.complete_json(
                system_prompt="system",
                user_prompt="user",
                schema_name="planner_decision",
                schema={"type": "object"},
            )
        )
    assert raised.value.category == "structured_output"
    assert raised.value.detail == "missing required fields: awaiting_continuation, request_details"
    assert "padding" not in raised.value.detail
    assert "untrusted" not in raised.value.detail


def test_prompt_examples_and_output_shape_match_provider_schema(db, scripted_llm):
    planner = PlannerService(db, scripted_llm([]))
    call = start_call(planner.calls)
    order = planner._order(call.order_id)
    slots = planner._slots(order.id)
    schema = _planner_schema()
    context = json.loads(planner._user_prompt(call, order, slots, AgentTurnRequest(text="Hello")))
    required = set(schema["required"])
    assert required == {
        "action",
        "awaiting_continuation",
        "request_details",
        "missing_fields",
        "message",
        "proposal",
        "escalation_reason",
        "confidence",
    }
    for field in required:
        assert field in PLANNER_OUTPUT_SHAPE

    proposal_schema = schema["$defs"]["PlanProposal"]["properties"]
    step_schema = schema["$defs"]["PlanStep"]["properties"]
    for example in context["decision_examples"]:
        decision = example["decision"]
        assert set(decision) == required
        assert PlannerDecision.model_validate(decision)
        proposal = decision.get("proposal")
        if proposal is None:
            continue
        assert set(proposal) == set(proposal_schema)
        for step in (proposal["primary"], proposal.get("fallback")):
            if isinstance(step, dict):
                assert set(step) == set(step_schema)
                for field in ("window_start", "window_end"):
                    if isinstance(step.get(field), str):
                        assert step[field].endswith("+05:30")


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


def test_groq_uses_low_reasoning_and_keeps_complete_plan_schema():
    schema = _planner_schema()

    def handler(request):
        payload = json.loads(request.content)
        assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
        assert payload["model"] == "openai/gpt-oss-120b"
        assert payload["reasoning_effort"] == "low"
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
            base_url="https://api.groq.com/openai/v1/",
            api_key="test-groq-key", model="openai/gpt-oss-120b",
            transport=httpx.MockTransport(handler),
        )
        try:
            result = await client.complete_json(system_prompt="system", user_prompt="Hello", schema_name="planner_decision", schema=schema)
            assert PlannerDecision.model_validate_json(result.text).action.value == "RESPOND"
            assert result.completion_tokens == 30
        finally:
            await client.aclose()

    asyncio.run(run())


def test_builder_uses_only_google_credentials():
    config = Settings(
        _env_file=None, gemini_api_key="test-google-key", llm_api_key="test-old-key",
        groq_api_key="test-old-key",
    )
    client = build_llm_client(config)
    assert client is not None
    assert isinstance(client, GeminiClient)
    assert "test-google-key" not in repr(config)

    def handler(request):
        assert request.headers["x-goog-api-key"] == "test-google-key"
        payload = json.loads(request.content)
        assert payload["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "minimal"
        assert "reasoning_effort" not in payload
        return httpx.Response(200, json={"candidates": [{
            "finishReason": "STOP", "content": {"parts": [{"text": "{}"}]},
        }]})

    client._pool._transport = httpx.MockTransport(handler)

    async def run():
        try:
            await client.complete_json(system_prompt="system", user_prompt="user", schema_name="test", schema={"type": "object"})
        finally:
            await client.aclose()

    asyncio.run(run())


def test_missing_google_key_does_not_fall_back_to_removed_credentials():
    config = Settings(
        _env_file=None, gemini_api_key="", llm_api_key="test-old-key",
        groq_api_key="test-old-key", nvidia_api_key="test-old-key",
    )
    assert build_llm_client(config) is None


@pytest.mark.parametrize("provider", ["nim", "vertex", "local"])
def test_removed_providers_are_rejected(provider):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_provider=provider)


@pytest.mark.parametrize("provider", ["groq", "openrouter", "ollama"])
def test_supported_free_providers_are_accepted(provider):
    assert Settings(_env_file=None, llm_provider=provider).llm_provider == provider


def test_each_provider_uses_only_its_own_key():
    groq = Settings(_env_file=None, llm_provider="groq", groq_api_key="groq-key",
                    gemini_api_key="google-key")
    client = build_llm_client(groq)
    assert isinstance(client, OpenAICompatibleClient)
    assert client._api_key == "groq-key"
    assert Settings(_env_file=None, llm_provider="groq", gemini_api_key="google-key").llm_provider == "groq"


def test_provider_without_its_own_key_disables_the_planner():
    config = Settings(_env_file=None, llm_provider="groq", gemini_api_key="google-key")
    assert build_llm_client(config) is None


@pytest.mark.parametrize("url", [
    "https://integrate.api.nvidia.com/v1",
    "https://api.groq.com/openai/v1",
    "http://generativelanguage.googleapis.com/v1beta",
    "https://generativelanguage.googleapis.com.example/v1beta",
])
def test_google_key_cannot_be_sent_to_another_provider(url):
    config = Settings(
        _env_file=None, llm_provider="gemini", gemini_api_key="test-google-key",
        llm_base_url=url, llm_model="gemini-3.1-flash-lite",
    )
    with pytest.raises(ValueError, match="Gemini base URL"):
        build_llm_client(config)


@pytest.mark.parametrize("unsafe", [False, True])
def test_groq_plan_still_requires_policy_and_explicit_confirmation(db, unsafe):
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
            base_url="https://api.groq.com/openai/v1",
            api_key="test-groq-key", model="openai/gpt-oss-120b",
            transport=httpx.MockTransport(handler),
        )
        try:
            planner = PlannerService(db, llm)
            call = start_call(planner.calls, 1043)
            result = await planner.handle_turn(call.id, AgentTurnRequest(text="Leave it with security" if unsafe else "Please hand it to me"))
            assert result.turn.verdict.valid == (not unsafe)
            assert not result.turn.customer_confirmed
            with pytest.raises(DomainError):
                planner.calls.commit_plan(call.id, "groq-before-confirmation")
            if not unsafe:
                confirmed = await planner.handle_turn(call.id, AgentTurnRequest(text="Yes please go ahead"))
                assert confirmed.turn.customer_confirmed
                committed = planner.calls.commit_plan(call.id, "groq-confirmed-plan")
                assert committed.state == "COMMITTED"
                assert planner.calls.commit_plan(call.id, "groq-confirmed-plan").reused
            assert len(requests) == 1
        finally:
            await llm.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("status", [429, 503])
def test_groq_busy_response_falls_back_without_retry_or_commit(db, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "synthetic busy response"}})

    async def run():
        llm = OpenAICompatibleClient(
            base_url="https://api.groq.com/openai/v1",
            api_key="test-groq-key", model="openai/gpt-oss-120b",
            transport=httpx.MockTransport(handler),
        )
        try:
            planner = PlannerService(db, llm)
            call = start_call(planner.calls, 1043)
            result = await planner.handle_turn(call.id, AgentTurnRequest(text="Please hand it to me"))
            assert result.planner.error_code == ("LLM_RATE_LIMIT" if status == 429 else "LLM_UNAVAILABLE")
            assert result.planner.wording_source == "template"
            assert not result.turn.customer_confirmed
            assert planner.calls.get_call(call.id).current_proposal_id is None
            assert len(requests) == 1
        finally:
            await llm.aclose()

    asyncio.run(run())
