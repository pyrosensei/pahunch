<div align="center">

# Pahunch

**A policy-bound voice agent that calls customers before risky deliveries — and refuses to break the courier's rules, even when the customer asks it to.**

Prisma v3.0 streaming STT · Timbre v3.0 TTS · NVIDIA NIM planner · FastAPI · SQLite

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/api-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Gnani](https://img.shields.io/badge/speech-Prisma%20%2B%20Timbre-8A2BE2.svg)](https://app.gnani.ai/voice)
[![NVIDIA NIM](https://img.shields.io/badge/planner-NVIDIA%20NIM-76B900.svg)](https://build.nvidia.com)
[![Live matrix](https://img.shields.io/badge/live%20matrix-19%2F21-2ea44f.svg)](#live-voice-matrix)

</div>

---

## 1. The problem

Before a high-risk delivery — an OTP parcel, a cash-on-delivery parcel, a high-value parcel — the courier has one question they cannot answer from a screen: **"can I hand this to the customer, or not?"**

Today that question is resolved by a phone call to a human, or by guessing on the doorstep. Both fail:

| Failure | What actually happens |
|---|---|
| The customer speaks Hinglish, code-mixed, with background street noise | The agent mis-hears the instruction, or asks the customer to repeat it three times |
| The customer asks for something the courier **cannot** do | The agent either refuses without an alternative (customer is annoyed, parcel comes back) or **agrees** (policy breach, wrong delivery, fraud surface) |
| The instruction is conditional | "Leave it with my neighbour if I'm not home" needs a named recipient, a window, and a rule check before it becomes an executable plan |
| The call ends | There is no audit trail. Nobody can prove what was agreed, who confirmed it, or which rule allowed it |

Every existing voice bot in this space optimises for *conversation*. Pahunch optimises for **a defensible, executable decision**: the LLM proposes a plan, deterministic policy code decides whether that plan is allowed, and the courier gets a card they can act on.

---

## 2. What Pahunch does

```
customer speaks  →  agent proposes a plan  →  policy validates  →  agent negotiates an allowed alternative
                                                                        ↓
                                            courier card + audit trail + committed plan
```

1. **Listens** — streaming Prisma STT over a live WebSocket, with barge-in (the customer can cut the agent off mid-sentence), VAD endpointing, and noise robustness.
2. **Proposes** — an LLM planner turns noisy Hinglish into a structured `PlanProposal` (primary + fallback, window, named recipient, retry rule). It never commits anything.
3. **Validates** — the policy engine checks the proposal against the order: OTP rules, COD rules, high-value thresholds, drop permissions, named-recipient requirements, slot availability. It returns a verdict and a list of **allowed alternatives**.
4. **Negotiates** — if the proposal is illegal, the agent explains *which rule* blocked it and offers a permitted alternative, instead of a flat refusal.
5. **Confirms** — an explicit, language-aware confirmation gate. No confirmation, no commit. Ever.
6. **Commits** — policy is **revalidated at commit time**, the plan and a human-readable courier card are written to SQLite, and the full audit trail is recorded.

---

## 3. The core idea: the LLM proposes, the policy disposes

This separation is the whole product.

```
┌──────────────────────────────┐
│  LLM planner (Nemotron 30B)  │  ← may only PROPOSE. Never decides legality.
└───────────────┬──────────────┘
                │  PlanProposal (primary + fallback)
                ▼
┌──────────────────────────────────────────────────────────────┐
│  Policy engine (pure Python, no LLM in the loop)             │
│  OTP · COD · high value · drop permissions · named recipient │
│  slot availability · escalation                                │
└───────────────┬──────────────────────────────────────────────┘
                │  PolicyVerdict(valid, violations, allowed_alternatives)
                ▼
┌──────────────────────────────────────────────────────────────┐
│  Commit layer: revalidate → write plan → courier card → audit │
└──────────────────────────────────────────────────────────────┘
```

Consequences that matter in production:

- **The LLM cannot grant permission.** It cannot talk its way into a policy violation, because the policy check happens after the model and again at commit time.
- **Every refusal is explainable.** Each violation carries a code, a message, and a path (`primary` / `fallback` / `order`), so the agent can say *why* and *what else is allowed*.
- **A plan can expire safely.** If the order changed between proposal and commit, the commit is refused (HTTP 409) and the call returns to negotiation instead of shipping a stale plan.

---

## 4. System design

### 4.1 The live voice path

```
┌────────────┐   PCM16 16 kHz    ┌──────────────────────────────────────────┐
│  Browser   │ ────────────────► │  FastAPI  /ws/sessions/{id}/voice       │
│  mic / PTT │ ◄──────────────── │                                          │
└────────────┘   agent audio     │  ┌────────────────────────────────────┐  │
       ▲                          │  │ VAD endpointing (energy + silence) │  │
       │                          │  ├────────────────────────────────────┤  │
       │                          │  │ Prisma v3.0 streaming STT (WS)     │  │
       │                          │  │  → text, detected language         │  │
       │                          │  ├────────────────────────────────────┤  │
       │                          │  │ Planner (NVIDIA NIM, JSON Schema)   │  │
       │                          │  ├────────────────────────────────────┤  │
       │                          │  │ Policy engine (deterministic)      │  │
       │                          │  ├────────────────────────────────────┤  │
       │                          │  │ Timbre v3.0 TTS → agent audio       │  │
       │                          │  ├────────────────────────────────────┤  │
       │                          │  │ State machine + audit + SQLite      │  │
       │                          │  └────────────────────────────────────┘  │
       └──────────────────────────┴──────────────────────────────────────────┘
```

**WebSocket protocol** (`/ws/sessions/{session_id}/voice`)

| Direction | Message | Purpose |
|---|---|---|
| client → server | `{"type":"start"}` / `{"type":"stop"}` / `{"type":"reset"}` | start / stop / reset the mic capture |
| client → server | binary PCM16 frames | 16 kHz mono audio |
| server → client | `{"type":"ready","session_id","state"}` | socket accepted |
| server → client | `{"type":"speech_start"}` / `{"type":"speech_end","duration_ms"}` | VAD events |
| server → client | `{"type":"turn", transcript, agent_message, timings, planner, ...}` | the processed turn |
| server → client | `{"type":"agent_audio","audio_base64","content_type"}` | synthesised agent speech |
| server → client | `{"type":"stop_speaking"}` | barge-in: the agent stops mid-playback |
| server → client | `{"type":"error","detail"}` | protocol or provider error |

Close codes `1013` (speech not configured) and `4404` (session not found).

### 4.2 Call state machine

```
CREATED → CALLING → CONNECTED → CONTEXT_CONFIRMATION → DISCOVERY
                                                       │        │
                                    ┌──────────────────┘        │
                                    ▼                           ▼
                              NEGOTIATION  ⇄  PENDING_CONFIRMATION
                                    │                │
                                    │                ▼
                                    │            COMMITTED
                                    ▼
   NO_ANSWER · UNREACHABLE · ESCALATED · TOOL_FAILURE · TURN_BUDGET_EXHAUSTED
```

Illegal transitions are refused with HTTP 409 and audited. `COMMITTED`, `NO_ANSWER`, `UNREACHABLE`, `ESCALATED`, `TOOL_FAILURE`, `TURN_BUDGET_EXHAUSTED` are terminal.

### 4.3 Policy rules

Every rule maps to a `ViolationCode` that the agent can explain out loud.

| Violation code | Trigger |
|---|---|
| `OTP_REQUIRED_NO_THIRD_PARTY` | Any third-party drop on an order that requires OTP |
| `COD_REQUIRES_DIRECT_HANDOFF` | Any third-party drop on a cash-on-delivery order |
| `HIGH_VALUE_DIRECT_HANDOFF` | Third-party drop on a parcel ≥ ₹25,000 |
| `SECURITY_DROP_NOT_ALLOWED` | `LEAVE_WITH_SECURITY` without safe-drop + security-drop permission |
| `NEIGHBOUR_DROP_NOT_ALLOWED` | `LEAVE_WITH_NEIGHBOR` without safe-drop + neighbour-drop permission |
| `NAMED_RECIPIENT_REQUIRED` | Neighbour drop without a named recipient |
| `SLOT_NOT_AVAILABLE` | Proposed window matches neither the order's own window nor an available slot |
| `MISSING_ORDER_METADATA` | Order has no delivery address |
| `ESCALATION_REQUIRED` | The plan requires a human |

The verdict also returns `allowed_alternatives` — the set of actions that *are* legal for this order — which is how the agent offers a real alternative instead of a dead end.

### 4.4 The planner

`PlannerDecision` is emitted as strict JSON Schema (`additionalProperties: false`, every property required) and parsed into a Pydantic model. Four actions: `ASK_CLARIFICATION`, `PROPOSE_PLAN`, `CONFIRM_PLAN`, `ESCALATE_TO_HUMAN`.

The model is **not trusted**. A layer of deterministic guards runs before and after it:

| Guard | Behaviour |
|---|---|
| `SAFETY_REPLY` | OTP requests and "are you a real person?" questions are answered from templates, with no LLM call |
| `POLICY_OVERRIDE` | "ignore the rules" (English + Devanagari variants) escalates immediately and is audited |
| `CONFIRMATION_RULE` | Explicit confirmations are recognised deterministically (Devanagari + English), no LLM call |
| `LOW_TRANSCRIPT_CONFIDENCE` / `LOW_CONFIDENCE` | Below-threshold understanding degrades to a safe re-ask instead of acting |
| `ACTION_NOT_ALLOWED` | The model cannot take an action that the current state forbids |
| `CONFIRMATION_WITHOUT_OFFER` | A bare "haan" after a clarification or degraded turn cannot commit |
| `PLAN_NORMALIZED` | `HAND_TO_CUSTOMER` for any window other than the order's own window is rewritten to `RESCHEDULE` |
| `safe_model_message` | Model output that is empty, over-long, or claims success ("done", "delivered", "OTP") is replaced with a safe line |

Invalid model output triggers **one repair call** with the validation error attached; a second failure degrades the turn rather than guessing.

### 4.5 Repository map

| File | Responsibility |
|---|---|
| `backend/app/main.py` | FastAPI app, all HTTP routes, static demo mount |
| `backend/app/realtime.py` | Voice WebSocket: VAD, barge-in, turn assembly, event emission |
| `backend/app/streaming_stt.py` | Per-connection Prisma streaming STT client (binary audio in, transcript out) |
| `backend/app/gnani.py` | Gnani REST client: batch STT + TTS |
| `backend/app/voice.py` | Speech orchestration: transcribe → plan → synthesise |
| `backend/app/planner.py` | LLM planner, prompt, guards, repair, normalisation |
| `backend/app/policy.py` | Pure policy engine — the only component allowed to say "allowed" |
| `backend/app/state_machine.py` | Call states, legal transitions, terminal states |
| `backend/app/calls.py` | Call lifecycle, turns, proposals, commits, idempotency, audit |
| `backend/app/tools.py` | Tool names and per-state tool permissions |
| `backend/app/models.py` | SQLModel entities: Order, DeliverySlot, CallSession, Turn, Proposal, Decision, Commit, AuditEvent |
| `backend/app/schemas.py` | Pydantic request/response contracts |
| `backend/app/llm.py` | OpenAI-compatible client with strict JSON Schema mode, timeouts, token accounting |
| `backend/app/seed.py` | Synthetic SwiftCart orders (1042–1049) covering every policy branch |
| `backend/app/config.py` | All settings, env-var driven |
| `backend/static/` | Browser voice demo (mic / push-to-talk, live transcript, plan verdict, courier card) |
| `evals/` | Deterministic scenarios, live planner cases, live voice matrix + reports |

---

## 5. Test & evaluation suites

| Suite | Command | Network | Proves |
|---|---|---|---|
| Unit + integration | `cd backend && uv run pytest` | none | Policy, state machine, planner guards, voice socket, streaming STT against fakes — **142 tests** |
| Deterministic scenarios | `cd backend && uv run python ../evals/runner.py` | none | End-to-end call flows with scripted plans and no LLM |
| Live planner cases | `cd backend && uv run python ../evals/planner_runner.py` | NVIDIA NIM | The model returns schema-valid decisions for fixed Hinglish turns |
| Live voice matrix | `cd backend && uv run python ../evals/matrix_runner.py` | Gnani + NVIDIA NIM | The real WebSocket voice path, policy, commit and audit trail, T01–T22 |

### Live voice matrix

The matrix drives the actual server over HTTP and WebSocket: it synthesises every customer turn with Gnani Timbre, streams it as 16 kHz PCM over the voice socket, reads planner and policy decisions from SQLite, commits through the real endpoint, and writes a report.

**Latest published run: 19 / 21 live scenarios pass.**

| Metric | Value |
|---|---|
| Policy violations committed | **0** |
| Agent success claims before commit | **0** |
| Live scenarios passed | 19 / 21 |
| Turns evaluated | 39 |
| Turn latency (server voice path) | P50 3.87 s · P95 13.47 s |
| Client-observed stop-to-turn | P50 5.40 s · P95 14.77 s |
| Stage P50/P95 | STT 1.10/2.38 s · planner 1.72/12.11 s · TTS 2.08/3.86 s |
| Degraded planner turns | 3 (`LLM_TIMEOUT`, provider stall) |

The two failing scenarios (T01, T04) fail **only** because their first planner call hit the 12 s provider timeout and the safe fallback consumed the turn — the degradation is counted in the report rather than hidden. Both scenarios pass when the provider responds normally.

Full report: [`evals/reports/matrix-latest.md`](evals/reports/matrix-latest.md) · raw JSON with per-turn transcripts, policy verdicts, timings and full audit trails in `evals/reports/matrix-latest.json`.

---

## 6. Problems we hit, and how we fixed them

These are the real engineering problems from building this, not a retrospective of easy parts.

**1. The provider stalls, and a stall is not a bug in your code.** The same planner prompt measured 0.7 s to 6.9 s across runs, and intermittently exceeded any sane deadline. We bounded it at 12 s, made the failure mode a *safe spoken fallback* instead of silence, and made the evaluation report count degraded turns explicitly (`Degraded planner turns: 3 (LLM_TIMEOUT: 3)`) rather than letting a provider hiccup masquerade as a logic defect.

**2. Devanagari nukta is not one codepoint.** Prisma returned `ढ़` as the precomposed `U+095D`, while our confirmation whitelist was written for `ढ` + `U+093C`. The live transcript was correct; our normalisation was not, so a legitimate "हाँ ठीक है आगे बढ़ाइए" was rejected as a non-confirmation. Fix: fold precomposed nukta to decomposed form *before* token mapping.

**3. Names are stored in the script the customer used.** We do not transliterate: the plan stores `शर्मा जी`, and the courier card says `Leave with neighbour: शर्मा जी`. Our own evaluation initially failed on that, which is the correct outcome — a test asserting the wrong script is a bug in the test.

**4. "Hand it to me" vs "reschedule" was ambiguous.** The planner sometimes emitted `HAND_TO_CUSTOMER` with a *different* window, which policy then rejected as an unavailable slot — technically valid, conversationally nonsense. We normalised the invariant: `HAND_TO_CUSTOMER` means the order's own current window, and every other window is `RESCHEDULE`. The rewrite is audited as `PLAN_NORMALIZED`.

**5. An order's own window is not always in `available_slots`.** The order's scheduled window is already deliverable, so the policy now accepts a window that equals the order's window *or* matches a slot row. Before this, a perfectly valid handoff looked illegal on orders whose slots only listed future alternatives.

**6. Refusals were explained in the wrong order.** Telling a customer "that time window is not available" when the real blocker was *COD parcels cannot be left with anyone* is technically true and completely useless. Violation messages are now ordered by severity: hard constraints (escalation, OTP, COD, high value, security, neighbour) are explained before slot availability.

**7. A bare "haan" must not commit anything.** Confirmation is only honoured when the last agent turn actually offered a plan (`PROPOSE_PLAN` / `CONFIRM_PLAN`). After a clarification or a degraded turn, "haan" produces `CONFIRMATION_WITHOUT_OFFER` — the call stays in `PENDING_CONFIRMATION` and nothing is committed.

**8. The customer can ask us to break the rules, and sometimes asks politely.** "ignore the rules" is matched deterministically (English and Devanagari variants, with filler words), escalated immediately without an LLM call, and written to the audit trail as `POLICY_OVERRIDE`.

**9. Streaming STT state leaked between calls.** A new voice connection inherited the previous connection's queue, partials and error state. Each connection now gets its own client with an explicit reset, so a stale sentinel cannot poison the next call.

**10. Our own harness can produce false failures.** One published-looking run failed T08 because it was launched from a branch that predated the scenario fix — the product was right, the expectation was stale. Lesson applied: published matrices are always run from merged `main`.

---

## 7. Getting it running

### Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A Gnani API key (Prisma + Timbre) and an NVIDIA NIM API key

### 1. Configure

```bash
cd backend
cp .env.example .env
```

Fill in these three values in `.env` — everything else has a working default:

```dotenv
GNANI_API_KEY=<your gnani key>
LLM_API_KEY=<your nvidia nim key>
LLM_BASE_URL=https://integrate.api.nvidia.com/v1
LLM_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
```

`.env` is gitignored. Keys are never committed and never printed.

### 2. Install and run

```bash
cd backend
uv sync
uv run uvicorn app.main:app --port 8000
```

Open **http://127.0.0.1:8000** — the browser demo lists the seeded SwiftCart orders, starts a call, streams your microphone (or push-to-talk) over the voice WebSocket, and shows the live transcript, the plan verdict, the audit log and the courier card. Press **Commit plan** to exercise the real commit path.

The database is created and seeded automatically on startup (8 synthetic orders, ids `1042`–`1049`, covering OTP, COD, high-value, neighbour-drop, missing-metadata and already-delivered cases).

### 3. Run the tests

```bash
cd backend
uv run pytest              # 142 tests, no network required
```

### 4. Run the evaluations

```bash
# deterministic scenarios, no network
cd backend && uv run python ../evals/runner.py

# live planner cases (NVIDIA NIM)
cd backend && uv run python ../evals/planner_runner.py

# live voice matrix (Gnani + NVIDIA NIM) — use a disposable database
cd backend
DATABASE_URL=sqlite:///./matrix.db uv run uvicorn app.main:app --port 8000
DATABASE_URL=sqlite:///./matrix.db uv run python ../evals/matrix_runner.py
DATABASE_URL=sqlite:///./matrix.db uv run python ../evals/matrix_runner.py --only T01,T16
```

The matrix runner **deletes all rows** in the target database before it starts. Point it only at a disposable database.

---

## 8. Models and APIs used

| Role | Model / API | Notes |
|---|---|---|
| Speech-to-text | **Prisma v3.0** (`POST /stt/v3`, streaming WebSocket `/stt/v3/stream`) | `indic-auto` language detection, candidate languages `hi-IN,en-IN`, 16 kHz PCM16 |
| Text-to-speech | **Timbre v3.0** (`POST /api/v1/tts/inference`) | Voice `Shivani`, language `hi-en` |
| Planner | **NVIDIA NIM** — `nvidia/nemotron-3.5-lightning-30b-a3b` | 30B MoE / 3B active, strict JSON Schema output, thinking disabled for latency |
| Backend | FastAPI, SQLModel (SQLAlchemy) on SQLite | WebSocket voice path, no external database service |
| Evaluation | NVIDIA NIM (planner) + Gnani Timbre + Prisma (speech) | 21 live scenarios over the real socket |

Built directly on the Gnani **API platform** (`app.gnani.ai/voice`) — Prisma and Timbre are called from our own service code. No hosted agent orchestrator owns the conversation.

---

## 9. What is real, and what is not

Honesty about this is deliberate — the evaluation claims only hold if the boundary is explicit.

| Real | Simulated / out of scope |
|---|---|
| Streaming Prisma STT over a live WebSocket, real barge-in and VAD | The customer is **synthesised** with Gnani Timbre — no human speaker, no telephone codec, no carrier jitter |
| Timbre TTS synthesising every agent reply | White Gaussian noise at 8 dB SNR stands in for recorded street audio |
| NVIDIA NIM producing the structured plan | Orders, customers and the SwiftCart merchant are **synthetic** |
| Real policy evaluation, state machine, commit, SQLite writes, audit trail | No real telephony: the browser page is the call surface, a telephony adapter would be optional |
| Real commit-time revalidation and idempotency | Commit is a local revalidation + SQLite write, not a remote tool — so T19's failure semantics are covered by tests instead of a live timeout |
| Real Prisma/Timbre/NIM calls in the live matrix | Prisma streaming STT emits **no confidence score**, so the live path relies on the explicit-confirmation gate; low-confidence rejection is covered by deterministic tests |

No real customer data, no real phone numbers, no OTP collection, and no impersonation of any real delivery company.

---

## 10. Known limits

- **Latency is the main open problem.** Current voice turn is P50 3.87 s / P95 13.47 s end-to-end, dominated by planner variance and full-sentence TTS synthesis. The target is a sub-second *time-to-first-audio*, which requires overlapping the stages rather than summing them.
- **Streaming STT gives no confidence**, so the live path cannot reject a low-confidence transcript by score; it compensates with the explicit-confirmation gate.
- **Planner stalls are external.** The 12 s bound is a product trade-off (silence on a call is worse than a safe re-ask), not a fix for the provider.
- **Single-order scope.** No multi-parcel routing, no courier-side app, no dispatch integration.
- **Evaluation scale.** 21 live scenarios, one planner model, one voice, one language mix. Changing the model, prompt, VAD thresholds or TTS settings invalidates comparisons with older reports.

## 11. What's next

- **Latency programme** — sub-second time-to-first-audio: overlapped speech-first planning, chunked TTS, tighter endpointing, a pre-synthesised fallback voice, and a smaller/faster planner model.
- **Operator dashboard** — live calls, plan verdicts, courier cards and audit trails.
- **Public deployment** — a hosted instance so judges can try it, with credits-aware guardrails.
- **Broader language coverage** and a telephony adapter.

---

## 12. Repository

```
pahunch/
├── backend/
│   ├── app/            # the service
│   ├── static/         # browser voice demo
│   ├── tests/          # 142 unit + integration tests
│   └── scripts/        # realtime smoke test, STT latency spike
├── evals/
│   ├── scenarios.json          # deterministic call scenarios
│   ├── planner_cases.json      # live planner cases
│   ├── matrix_scenarios.json   # live voice matrix T01–T22
│   ├── runner.py               # deterministic scenario runner
│   ├── planner_runner.py       # live planner runner
│   ├── matrix_runner.py        # live voice matrix runner
│   └── reports/                # published matrix reports
└── .github/            # (in progress)
```

Built with [FastAPI](https://fastapi.tiangolo.com/), [SQLModel](https://sqlmodel.tiangolo.com/), [httpx](https://www.python-httpx.org/), [uv](https://docs.astral.sh/uv/), and the [Gnani AI](https://app.gnani.ai/voice) API platform.

## License

MIT — see [LICENSE](LICENSE).

## Feature stack snapshot

- `feat/runtime-contracts`: see the focused commits on this stacked branch.
- `feat/conversation-safety`: see the focused commits on this stacked branch.
- `feat/call-scheduling`: see the focused commits on this stacked branch.
- `fix/voice-session-runtime`: see the focused commits on this stacked branch.
- `fix/voice-session-runtime`: see the focused commits on this stacked branch.
- `feat/guarded-phone-adapter`: see the focused commits on this stacked branch.
