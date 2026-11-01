> Historical review: Gemini has been removed from the active configuration.
> See [GROQ_REVIEW.md](GROQ_REVIEW.md) for the current planner setup.

# Gemini planner configuration review

1 November 2026. The user selected `gemini-3.1-flash-lite` and entered its API
key locally. Work is isolated on `feat/gemini-planner`, based on the merged
`origin/main` at `cd0ed90`.

## Configured behavior

- `LLM_PROVIDER=gemini` selects Google's HTTPS compatibility endpoint and the
  chosen stable model. The actual local file also selects JSON Schema output
  and a 512-token completion budget.
- Gemini reads only `GEMINI_API_KEY`. The existing `LLM_API_KEY` remains available
  for a deliberate NIM configuration; there is no credential or model fallback.
  A missing Gemini key leaves the planner unconfigured. Gemini mode rejects
  other hosts, HTTP URLs and unexpected endpoint paths before sending a request.
- Gemini requests use `reasoning_effort=minimal`. The NIM-specific
  `chat_template_kwargs` is omitted. Gemini 3 cannot disable thinking entirely.
- The strict provider schema, local Pydantic validation, single schema-repair
  attempt, delivery policy, explicit confirmation, commit revalidation and
  idempotency remain intact. No multilingual plan parser was added.
- Gnani Prisma/Timbre continue to handle speech. No additional model call,
  search, grounding, new dependency or UI redesign was introduced.

Google references: [compatibility API and thinking mapping](https://ai.google.dev/gemini-api/docs/openai),
[structured output](https://ai.google.dev/gemini-api/docs/structured-output),
[selected model](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite).

## Live verification and current blocker

The key was checked through `app.config.settings`; its value was never printed
or copied into tracked files. The user filled the initially empty local field.
Configuration updates used `dotenv.set_key` for non-secret fields and preserved
the other credential entries. The Gemini credential is also excluded from the
Settings representation.

The selected model's metadata endpoint returned **HTTP 200** with the saved key.
Generation returned **HTTP 503 / UNAVAILABLE**, reporting high demand. A minimal
native `generateContent` control request also returned **503**, as did requests
through the compatibility endpoint. This availability problem is therefore not
limited to the compatibility request path.

**Successful real Gemini plan generation, exact live-schema compatibility,
conversation quality and latency remain unverified.** The backend safely returns
its existing unavailable wording and commits nothing on this failure. The live
eight-scenario voice matrix was not launched against a provider returning 503.
No faster-response claim is made.

Initial restricted-network attempts raised `ConnectError`; authorized network
execution reached Google. The diagnostic helper initially failed to handle an
array-shaped error body; its safe error extraction was corrected. That helper
is ignored and is not part of the production response parser.

Real Gnani checks continued to pass in this session:

| Check | Observation |
|---|---|
| Streamed raw PCM16 / 16 kHz | First audio 574 ms; complete 1181 ms |
| Streamed native mu-law / 8 kHz | First audio 424 ms; complete 594 ms |
| Blocking native mu-law / 8 kHz | Ready 642 ms; 22,400 bytes; no WAV header |
| REST STT on synthesized English confirmation | Correct recognized confirmation; 673 ms |

These are individual synthetic provider checks, not headset or carrier results.
No real Twilio request or call was made. Existing databases were not reset.

## Local verification

Commands below ran from `backend` with the existing virtual environment.

| Command | Observed result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider --basetemp=.artifacts/pytest-gemini-before-20261101` | Before changes: 369 passed, 1 warning, 77.22 s |
| `.\.venv\Scripts\python.exe -m pytest tests/test_llm_gateway.py -q -k 'gemini or chosen' -o addopts= -p no:cacheprovider --tb=short` | Before adapter changes: 6 failed, 1 passed, 6 deselected |
| `.\.venv\Scripts\python.exe -m pytest tests/test_llm_gateway.py tests/test_planner.py tests/test_conversation.py -q -o addopts= -p no:cacheprovider --tb=short` | 124 passed, 3.16 s |
| First full post-adapter suite | 376 passed, 1 warning, 72.37 s, before three additional policy/failure cases |
| `.\.venv\Scripts\python.exe -m pytest tests/test_llm_gateway.py -q -o addopts= -p no:cacheprovider --basetemp=.artifacts/pytest-gemini-policy-fixed --tb=short` | 16 passed, 0.47 s |
| `.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider --basetemp=.artifacts/pytest-gemini-release-20261101 --tb=short` | Final: 379 passed, 1 warning, 73.13 s |
| `node --test --test-reporter=tap tests/voice_browser.test.cjs tests/demo_browser.test.cjs` | 23 passed, 0 failed; browser/DOM doubles |
| `.\.venv\Scripts\python.exe ..\evals\runner.py` | 12/12 scripted scenarios passed; in-memory database |
| `.\.venv\Scripts\ruff.exe check app tests --no-cache` and `git diff --check` | Passed; Git emitted line-ending normalization notices |

The additional integration cases exercise an actual Gemini-mode HTTP client
against a mocked provider: valid proposals cannot commit before explicit yes,
COD security drops are rejected, confirmed commits are idempotent, and a 503
produces a safe fallback after one request without a new proposal or commit.
They do not substitute for real Gemini recognition/planning measurements.

One new integration fixture initially used the wrong order's delivery window;
policy correctly rejected it. The fixture was corrected to use the actual COD
order's window, without weakening the assertion or policy. A subsequent import
ordering lint error was corrected. The suite warning is the existing Starlette
TestClient/httpx deprecation. No dependency was installed.

Live commands were `.\.venv\Scripts\python.exe .artifacts/provider_probe.py` and
`.\.venv\Scripts\python.exe .artifacts/gemini_probe.py`. Scratch scripts, results
and synthetic databases remain ignored.

## Files and remaining work

Changed files: `backend/app/config.py`, `backend/app/llm.py`,
`backend/tests/test_llm_gateway.py`, `backend/.env.example`, `README.md`, and
this review. The actual `.env` is local and excluded from Git. The user's
pre-existing local deletion of `AGENTS.md` is left untouched.

When Google generation is available again, rerun the complete-plan probe,
English/Hindi/Hinglish and policy scenarios, then compare substantive audio
timing with the historical NIM measurements. Headset checks and the later
approved handset call remain separate validation steps. No alternate model was
silently selected to work around Google's overload.
