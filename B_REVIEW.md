# Stage B review

31 October 2026. Implemented after the owner requested the next step following
the A2 report. Work remains on `feat/voice-latency-and-demo`, uncommitted. Prior
A1/A2 changes are preserved. This pass covers contextual wording and language;
the automatic dialer, shop/dashboard APIs and Twilio integration are not built.

## Result

- Added `RESPOND` for greetings and questions, with no proposal or commit tool.
  Scripted multi-turn coverage exercises a greeting, a factual question, a valid
  proposal, explicit confirmation and the existing commit gate.
- Proposal wording accepts only recognized wrappers around `{plan_summary}` and
  `{confirmation_question}`. The backend inserts the policy-validated action,
  date/window, named recipient, arrival-call instruction and fallback. Unknown,
  contradictory, unsafe and wrong-language wording uses a localized deterministic
  fallback. No multilingual natural-language parser was added.
- Window/payment/policy questions use server-filled factual templates. Free
  conversational responses have conservative safety and language filters.
- Added `response_language`, `wording_source` and `fallback_reason` to planner
  metadata, persisted assistant turns, assistant audits and voice audits. The
  browser displays them and renders transcript/model content with `textContent`.
- English, Devanagari Hindi and Roman Hinglish selection now reaches REST TTS,
  streamed PCM, blocking fallback and cache keys. Cached acknowledgements must
  match both language and sample rate; the opening greeting remains Hinglish.
- Questions clear old confirmation. Degraded replies cannot make stale proposals
  confirmable. Rejected unchanged plans are blocked. Clarification turns consume
  the existing clarification budget, and terminal outcomes use terminal wording.
  The explicit-confirmation vocabulary and policy-at-commit checks are unchanged.
- Recognized volunteered OTPs are redacted before model use, persistence and
  returned voice transcripts. Typed input waits for redacted backend output.
  Common English OTP/identity questions bypass the model. Recipient text cannot
  insert an OTP request into a server-rendered proposal.

`wording_source=model` means the model's wording or selected template was
accepted. It does not mean the model supplied trusted plan/order facts.

## Files changed in Stage B

| Files | Purpose |
|---|---|
| `backend/app/language.py` (new) | Script/token language selection, localized lines, wording contract and OTP redaction |
| `backend/app/planner.py` | RESPOND routing, prompt/context contract, deterministic fact insertion, grounding and safety guards, wording audits |
| `backend/app/schemas.py`, `backend/app/tools.py` | Additive action, metadata and state permissions |
| `backend/app/calls.py` | Confirmation clearing, clarification accounting and persistence redaction |
| `backend/app/gnani.py`, `backend/app/voice.py`, `backend/app/realtime.py` | Per-turn TTS language, language-specific cache/ack handling and redacted voice output |
| `backend/static/index.html` | Safe text display, wording metadata and typed-input redaction handling |
| `backend/tests/test_conversation.py` (new), `backend/tests/test_voice_language.py` (new) | Conversation, safety, grounding, language, cache and speech-path regressions |
| `backend/tests/conftest.py`, `backend/tests/test_realtime.py`, `backend/tests/test_realtime_a1.py` | Speech doubles accept the additive language keyword |
| `backend/tests/test_planner.py` | Safety reply action is now RESPOND; existing safety assertions retained |
| `backend/tests/voice_browser.test.cjs` | Two browser regressions for safe rendering/metadata and delayed typed-input display |
| `README.md`, `VOICE_REPAIR_ANALYSIS.md`, `B_REVIEW.md` | Updated behavior, authorization scope, test counts and review record |

Other working-tree changes belong to A1/A2 or the supplied task/instruction files.
No dependencies, CI configuration, credentials or database files were changed.

## Verification

Commands below ran from `backend` using the installed virtual environment.

Full suite, before this stage and after implementation:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider
```

Baseline: **198 passed, 1 warning in 61.61s**. Final: **255 passed, 1 warning
in 64.02s**, including 57 new Python cases. The warning is the existing
Starlette TestClient/httpx deprecation.

Regression failures were exercised before their corresponding repairs:

- Initial conversation tests: **32 failed**, against the A2 implementation.
- Initial speech-language tests: **10 failed** before language propagation.
- Initial browser run: **12 passed, 2 failed** before safe rendering and metadata.
- An intermediate planner/call regression run reported **2 failed, 96 passed**:
  the old safety-action expectation needed the approved RESPOND action, and an
  overbroad OTP detector incorrectly redacted a normal delivery-time instruction.
  The action assertion was updated openly; the detector was narrowed. The same
  selection then passed **98 tests**.
- English safety additions reported **7 failed, 39 passed** before fixing the
  OTP/identity fast paths and unsafe free wording. Conversation plus existing
  planner tests then passed **102 tests**.
- Recipient OTP-injection regression reported **1 failed, 46 deselected** before
  rejecting OTP-bearing recipient text. An initial test phrasing took the OTP
  fast path already, so it was corrected to reach proposal rendering.

Browser regressions:

```powershell
node --test --test-reporter=tap tests/voice_browser.test.cjs
```

**14 passed, 0 failed**. These use browser API/DOM/worklet fakes, not a microphone
or real audio output device.

Deterministic evaluation:

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe ../evals/runner.py
```

**12/12 scenarios passed**. These use scripted proposals and in-memory SQLite;
they are not speech recognition or live model measurements.

Static checks:

```powershell
.\.venv\Scripts\ruff.exe format app/language.py app/planner.py tests/test_conversation.py
.\.venv\Scripts\ruff.exe check app/language.py app/planner.py app/schemas.py app/tools.py app/calls.py app/gnani.py app/voice.py app/realtime.py tests/test_conversation.py tests/test_voice_language.py tests/conftest.py tests/test_planner.py tests/test_realtime.py tests/test_realtime_a1.py --no-cache
```

**All checks passed.** Initial lint findings (imports, unused names and literal
construction) were corrected. `git diff --check` passed from the repo root;
Git emitted existing line-ending and global-ignore permission warnings during
inspection. No tracked whitespace errors were reported.

## Measurements and limits

- Three language branches exercise accepted proposal wrappers with server-filled
  facts. Three REST voice cases mirror transcript text despite a conflicting STT
  language tag. Stream/blocking fallback and language-separated cache requests
  are verified using speech doubles and mocked HTTP payloads.
- No live NVIDIA NIM or Gnani request was made. Actual model adherence to the
  wrapper contract, voice pronunciation and provider language support remain
  unverified in this stage. Live `first_audio_ms`, handset performance and
  acoustic echo were not measured; no latency improvement is claimed.
- Language detection is a script/token heuristic. Short or ambiguous mixed
  turns can select the wrong language. The first greeting has no customer
  language to mirror. An uncached matching acknowledgement is skipped rather
  than playing one in another language.
- Free RESPOND filtering is conservative, not a semantic proof over arbitrary
  language. Critical plan facts use the exact placeholder contract. Recipient
  grounding currently requires the name in the current customer turn; references
  to a name only in an earlier turn may need clarification.
- Redaction recognizes common OTP labels, 4–8 digit codes and common consecutive
  spelled-out number words. Unusual obfuscation/transcription errors are not
  exhaustively covered. Input audio itself is still sent to STT for recognition.
- Historical live-matrix results in README were not rerun. Existing fixture
  dates remain fixed; dynamic order windows and automatic dispatch belong to
  later stages. Key count is not evidence of independent provider capacity.
- No automatic dialer, Twilio connection, live call, dependency installation,
  push or external publication was performed.
