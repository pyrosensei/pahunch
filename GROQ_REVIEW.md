# Groq planner configuration review

Date: 1 November 2026

## Current configuration

Groq is the only active planner provider. Speech recognition and synthesis
continue to use Gnani Prisma and Timbre.

```dotenv
LLM_PROVIDER=groq
GROQ_API_KEY=
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=openai/gpt-oss-20b
LLM_TIMEOUT_SECONDS=12
LLM_MAX_TOKENS=1024
LLM_STRUCTURED_MODE=json_schema
```

The adapter sends `reasoning_effort=low`, `include_reasoning=false`, and
`max_completion_tokens=1024`. This output cap includes reasoning tokens; it
does not request a long answer. Strict JSON Schema remains enabled. No second
model, SDK installation, or additional normal-turn request was introduced.

The real ignored `backend/.env` has the connection values and the user's saved
`GROQ_API_KEY`. The key was verified through a successful live planner request.
Gemini, Google, NVIDIA/NIM, generic old planner credentials, and the
old thinking flag were removed from that file. Gnani and Twilio settings were
preserved. No credential value was printed or committed.

## Backend and safety

- Removed Gemini/NIM settings, provider branches, and credential fallbacks.
- Only the official Groq HTTPS compatibility endpoint (or its complete
  chat-completions URL) is accepted, preventing accidental key forwarding.
- Missing Groq credentials disable the live planner; old credentials cannot
  substitute for them. Old provider selections are rejected.
- Existing local schema, policy and wording checks, explicit confirmation,
  commit-time revalidation and idempotency remain in force.
- HTTP 429 and 503 produce the existing safe unavailable response without a
  blind retry, a pending proposal, or a commit.

The README, environment template, and live-evaluation instructions now describe
Groq. Earlier provider reports remain historical records and do not establish
Groq response quality or latency.

## Validation

- Focused gateway regressions: **19 passed**.
- Full Python suite: **382 passed**, one existing Starlette/httpx deprecation
  warning, in **74.91 seconds**.
- Browser/DOM mock tests: **23 passed**; no physical microphone check.
- Scripted local call scenarios: **12/12 passed**, with no provider calls.
- Ruff: passed. Git whitespace check: passed.
- Actual environment check: new field exists, old fields absent, existing speech
  and telephony credentials retained; only boolean results were displayed.

Live generation now succeeds with the saved Groq key and complete planner
schema. All **8 focused synthetic voice scenarios passed across 14 turns**.
The 9 model requests returned HTTP 200 without repairs; model request times
were 661-917 ms (median 824 ms). Model-driven turns reached first substantive
audio in 1,479-2,144 ms (median 1,654 ms) from the server utterance endpoint.
These measurements exclude silence detection and physical playback. There
were no provider or audio errors in this small paced run.

The observed account limit is 8,000 tokens/minute and 1,000 requests/day, with
roughly 3,500 input tokens per model turn. Tests were deliberately paced outside
the measured turn to avoid exceeding that quota; this does not establish
rapid-conversation or concurrent-call capacity. Current local regression results,
language findings and exact limits are recorded in
[GROQ_LIVE_REVIEW.md](GROQ_LIVE_REVIEW.md). No real handset call was made.

## Provider documentation

- [OpenAI-compatible endpoint](https://console.groq.com/docs/overview)
- [Strict structured outputs and supported models](https://console.groq.com/docs/structured-outputs)
- [Reasoning settings](https://console.groq.com/docs/reasoning)
- [Organization limits](https://console.groq.com/docs/rate-limits)

Groq documents strict schema support for GPT-OSS 20B and low reasoning effort.
Organization quotas still apply; adding keys does not increase those limits.
Sub-second end-to-end responses are a measurement target, not a guarantee.
