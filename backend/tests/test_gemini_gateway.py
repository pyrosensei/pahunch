import asyncio
import json
from datetime import UTC

import httpx
import pytest
from test_planner import db as planner_database
from test_planner import start_call, valid_proposal

from app.errors import DomainError
from app.llm import GeminiClient, LLMResponseError, LLMTimeoutError
from app.models import Order
from app.planner import PlannerService, _planner_schema
from app.schemas import AgentTurnRequest

db = planner_database


def gemini_reply(text="{}", *, finish_reason="STOP", thoughts=None):
    parts = []
    if thoughts is not None:
        parts.append({"thought": True, "text": thoughts})
    parts.append({"text": text})
    return {
        "modelVersion": "gemini-3.1-flash-lite",
        "candidates": [{"finishReason": finish_reason, "content": {"parts": parts}}],
        "usageMetadata": {
            "promptTokenCount": 101, "candidatesTokenCount": 22, "thoughtsTokenCount": 3,
        },
    }


def make_client(handler, **kwargs):
    return GeminiClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="synthetic-google-key",
        model="gemini-3.1-flash-lite",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


async def complete(client, schema=None):
    try:
        return await client.complete_json(
            system_prompt="system", user_prompt="user",
            schema_name="planner_decision", schema=schema or {"type": "object"},
        )
    finally:
        await client.aclose()


def test_native_google_request_keeps_planner_schema_and_uses_header_authentication():
    schema = _planner_schema()

    def handler(request):
        assert str(request.url) == (
            "https://generativelanguage.googleapis.com/v1beta/"
            "models/gemini-3.1-flash-lite:generateContent"
        )
        assert request.headers["x-goog-api-key"] == "synthetic-google-key"
        assert "authorization" not in request.headers
        assert not request.url.query
        payload = json.loads(request.content)
        assert payload["systemInstruction"] == {"parts": [{"text": "system"}]}
        assert payload["contents"] == [{"role": "user", "parts": [{"text": "user"}]}]
        generation = payload["generationConfig"]
        assert generation["responseMimeType"] == "application/json"
        assert generation["responseJsonSchema"] == schema
        assert "responseSchema" not in generation
        assert generation["thinkingConfig"] == {"thinkingLevel": "minimal"}
        assert generation["maxOutputTokens"] == 1024
        assert generation["temperature"] == 1.0
        assert "reasoning_effort" not in payload
        return httpx.Response(200, json=gemini_reply('{"ok":true}', thoughts="hidden thought"))

    completion = asyncio.run(complete(make_client(handler), schema))
    assert completion.text == '{"ok":true}'
    assert completion.model == "gemini-3.1-flash-lite"
    assert completion.prompt_tokens == 101
    assert completion.completion_tokens == 25
    assert completion.latency_ms >= 0


def test_native_json_object_mode_keeps_json_mime_type():
    def handler(request):
        generation = json.loads(request.content)["generationConfig"]
        assert generation["responseMimeType"] == "application/json"
        assert "responseJsonSchema" not in generation
        return httpx.Response(200, json=gemini_reply())

    assert asyncio.run(complete(make_client(handler, structured_mode="json_object"))).text == "{}"


@pytest.mark.parametrize(("reply", "category"), [
    (gemini_reply('{"partial":', finish_reason="MAX_TOKENS"), "truncated_output"),
    (gemini_reply(finish_reason="SAFETY"), "blocked_output"),
    ({"candidates": [{"finishReason": "SAFETY"}]}, "blocked_output"),
    ({"promptFeedback": {"blockReason": "SAFETY"}}, "blocked_output"),
    ({"candidates": []}, "invalid_response"),
    (gemini_reply("", thoughts="private reasoning"), "empty_output"),
])
def test_native_incomplete_or_empty_output_cannot_be_used(reply, category):
    client = make_client(lambda request: httpx.Response(200, json=reply))
    with pytest.raises(LLMResponseError) as error:
        asyncio.run(complete(client))
    assert error.value.category == category


def test_native_timeout_preserves_gateway_classification():
    def handler(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    with pytest.raises(LLMTimeoutError):
        asyncio.run(complete(make_client(handler)))


@pytest.mark.parametrize("model", ["../other-model", "gemini-3.1-flash-lite?key=oops"])
def test_model_code_cannot_change_the_google_request_path(model):
    with pytest.raises(ValueError, match="Gemini model"):
        GeminiClient(
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key="synthetic-key", model=model,
        )


@pytest.mark.parametrize("status", [400, 401, 403, 429, 503])
def test_google_failure_is_not_retried_or_committed(db, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {
            "code": status, "status": "RESOURCE_EXHAUSTED" if status == 429 else "ERROR",
            "message": "synthetic failure",
        }})

    async def run():
        client = make_client(handler)
        try:
            planner = PlannerService(db, client)
            call = start_call(planner.calls, 1043)
            result = await planner.handle_turn(call.id, AgentTurnRequest(text="Keep the original time"))
            assert result.planner.error_code == (
                "LLM_RATE_LIMIT" if status == 429 else "LLM_UNAVAILABLE"
            )
            assert result.planner.degraded
            assert not result.turn.customer_confirmed
            assert planner.calls.get_call(call.id).current_proposal_id is None
            assert "preferred time" not in result.agent_message.lower()
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("unsafe", [False, True])
def test_native_google_plan_still_requires_policy_and_explicit_confirmation(db, unsafe):
    proposal = valid_proposal()
    order = db.get(Order, 1043)
    proposal["primary"].update(
        window_start=order.window_start.replace(tzinfo=order.window_start.tzinfo or UTC).isoformat(),
        window_end=order.window_end.replace(tzinfo=order.window_end.tzinfo or UTC).isoformat(),
    )
    if unsafe:
        proposal["primary"]["action"] = "LEAVE_WITH_SECURITY"
    output = {
        "action": "PROPOSE_PLAN", "awaiting_continuation": False,
        "request_details": None, "missing_fields": [],
        "message": "{plan_summary} {confirmation_question}",
        "proposal": proposal, "escalation_reason": None, "confidence": 0.95,
    }

    async def run():
        client = make_client(lambda request: httpx.Response(200, json=gemini_reply(json.dumps(output))))
        try:
            planner = PlannerService(db, client)
            call = start_call(planner.calls, 1043)
            result = await planner.handle_turn(call.id, AgentTurnRequest(
                text="Leave it with security" if unsafe else "Please hand it to me",
            ))
            assert result.turn.verdict.valid == (not unsafe)
            assert not result.turn.customer_confirmed
            with pytest.raises(DomainError):
                planner.calls.commit_plan(call.id, "google-before-confirmation")
            if not unsafe:
                result = await planner.handle_turn(call.id, AgentTurnRequest(text="Yes please go ahead"))
                assert result.turn.customer_confirmed
                committed = planner.calls.commit_plan(call.id, "google-confirmed-plan")
                assert committed.state == "COMMITTED"
                assert planner.calls.commit_plan(call.id, "google-confirmed-plan").reused
        finally:
            await client.aclose()

    asyncio.run(run())
