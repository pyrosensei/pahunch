<div align="center">

# Pahunch

**A policy-bound voice agent that calls customers before risky deliveries — and refuses to break the courier's rules, even when the customer asks it to.**

Prisma v3.0 streaming STT · Timbre v3.0 TTS · Gemini 3.1 Flash-Lite planner · FastAPI · SQLite · React 19

[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/api-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Gnani](https://img.shields.io/badge/speech-Prisma%20%2B%20Timbre-8A2BE2.svg)](https://app.gnani.ai/voice)
![Planner](https://img.shields.io/badge/planner-Gemini%203.1%20Flash--Lite-1A7CE7.svg)
[![Tests](https://img.shields.io/badge/backend%20tests-698%20passing-2ea44f.svg)](#test--evaluation-suites)
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

1. **Listens** — streaming Prisma STT over a live WebSocket, with calibrated energy detection, retained onset audio and sustained-speech barge-in. Real microphone and acoustic echo performance remain to be measured.
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
│  LLM planner (OpenRouter)      │  ← may only PROPOSE. Never decides legality.
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
       │                          │  │ Planner (Gemini, JSON Schema)    │  │
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
| client → server | `{"type":"config","tts_sample_rate":48000,"playback_tracking":true,"audio_feedback":true}` | configure browser output rate, playback acknowledgements and microphone feedback |
| client → server | `{"type":"playback_done","generation_id":1}` | acknowledge the final rendered sample of the current output generation |
| client → server | `{"type":"scripted_turn","text":"..."}` | typed/scripted input; bypasses STT, uses the real planner/policy path |
| server → client | `{"type":"ready","session_id","state"}` | socket accepted |
| server → client | `{"type":"speech_start"}` / `{"type":"speech_end","duration_ms"}` | VAD events |
| server → client | `{"type":"turn", transcript, agent_message, timings, planner, ...}` | the processed turn |
| server → client | `{"type":"agent_audio","audio_base64","content_type"}` | synthesised agent speech |
| server → client | `{"type":"agent_audio_chunk","chunk_base64","sample_rate","generation_id"}` | streamed PCM16 output |
| server → client | `{"type":"agent_audio_end","generation_id",timings,...}` | synthesis ended; playback may still be queued |
| server → client | `{"type":"stop_speaking","generation_id"}` | cancel and flush that output generation |
| server → client | `{"type":"error","detail"}` | protocol or provider error |
| server → client | `{"type":"audio_level",rms,noise_floor,threshold,calibrating,noise_limited,received_frames,speech}` | throttled detector feedback when audio_feedback is enabled |
| server → client | `{"type":"no_speech",reason,elapsed_ms,...}` | actionable missing-audio, quiet-input, high-noise, stopped-stream or too-short-input hint |

Output `turn`, audio chunks/blobs and audio-end messages carry `generation_id`.
The browser worklet distinguishes an underrun from final drain, and uses a local
connection ID so delayed callbacks cannot affect a replacement socket. Old clients
may omit `playback_tracking`; a subsequent `start` remains their playback handoff.
With tracking enabled, an early, stale or duplicate acknowledgement cannot release
the active generation. `start` during generation is deferred; `reset` cancels it.

The receive loop stays available while STT, planning and synthesis run. Frames
received while a turn is busy and has not started playback are dropped and counted
in `REALTIME_FRAMES_DROPPED`, rather than queued into the next utterance. Barge-in
invalidates output before cancelling its tasks, so late chunks cannot restart it.
STT has at most 50 queued frames and one second of queued PCM (excluding one
in-flight send); provider operations have explicit deadlines. Overflow or failure
uses the captured utterance for REST recognition, with an audited phase and a
`stt_fallback_reason` on the turn. Provider transcript queues hold at most 16
segments and fail visibly on overflow.

One socket owns each call session in a single application process. A duplicate is
audited and rejected before greeting or provider connection. Close codes are
`1013` (voice not configured), `4404` (session not found), and `4409` (duplicate
socket). Disconnect awaits turn, planner, greeting and STT cleanup;
the browser also releases microphone tracks. It leaves the logical call row
available for deliberate reconnect during the scheduler's disconnect grace period.
The scheduler then records an explicit timeout disposition. Cross-process socket
ownership is not implemented.

The microphone detector calibrates on the first 300 ms of listening audio. Stay
quiet briefly when the page displays its adjustment hint. It uses a lower
quantile of recent idle-frame RMS, a 3x noise margin and bounded thresholds
(120–12000 PCM16 RMS by default). Noise learning pauses during candidate speech,
an utterance and agent playback. Calibration persists between turns on the same
socket. A fresh socket calibrates again; speech covering the entire calibration
period can be mistaken for background noise and may need repeating.

Normal onset requires 100 ms of sustained energy; barge-in requires 160 ms.
The detector retains 200 ms before onset and sends that audio, including the
candidate speech, to STT. Idle/calibration frames are not sent to STT. Short
rejected utterances are audited, reset and reported to the page without a planner
call. The utterance cap counts actual buffered samples, including pre-roll and
pauses, and cannot overrun on a large frame.

**Two different silence defaults exist, and the probe below measures the smaller
one.** The running server waits `REALTIME_SILENCE_MS = 1000` (and
`REALTIME_QUIET_MS = 1000`) from `backend/app/config.py`, copied through
`build_realtime_config()`. The `RealtimeConfig` dataclass in
`backend/app/realtime.py` still defaults to `600`, and `scripts/vad_probe.py`
constructs it directly, so the probe measures the 600 ms dataclass default —
not the deployed 1000 ms window.

The page displays server detection status beside its local microphone meter.
Feedback is throttled to 250 ms. After five seconds without usable speech it
distinguishes no frames, no speech, excessive background noise and stopped capture;
the hint/audit is emitted once per listening episode, and does not create a
customer turn. Thresholds and timings are configurable with the `REALTIME_*`
settings in `.env.example`; `REALTIME_ADAPTIVE_VAD=false` selects the explicit
fixed-threshold mode using `REALTIME_VAD_THRESHOLD`. While a reply is being
prepared, `REALTIME_BARGE_IN_MS` at the fixed threshold confirms a strong
interruption, and quieter speech is confirmed over `REALTIME_PROCESSING_CONFIRM_MS`
so a short noise burst does not cancel the turn. Existing `.env` files need
no new entries to use the defaults.

Offline check, from `backend` in PowerShell:

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe scripts/vad_probe.py
```

This generates PCM, not speech recognition. Re-run on 9 November 2026 at
dataclass defaults: after a one-second ambient prelude, the quiet, normal and
steady-noise speech-level cases all measured 100 ms onset, 600 ms endpoint
delay and 0 false onsets; the steady-noise-only case produced no onset at all.
These are audio-sample timing measurements at a 600 ms silence window, not
microphone/provider latency and not the deployed 1000 ms window.
Before/after results and remaining limits: [A2 review](A2_REVIEW.md).

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

`PlannerDecision` is emitted as strict JSON Schema (`additionalProperties: false`, every property required) and parsed into a Pydantic model. Five actions: `RESPOND`, `ASK_CLARIFICATION`, `PROPOSE_PLAN`, `CONFIRM_PLAN`, `ESCALATE_TO_HUMAN`. `RESPOND` answers greetings and questions without creating a proposal or granting permission to commit.

For a proposal, the model selects a recognized contextual wrapper around
`{plan_summary} {confirmation_question}`. The backend inserts the policy-validated
action, window, recipient and fallback; unknown, contradictory, unsafe or
wrong-language wrappers use deterministic wording. Delivery-window, payment and
handoff-policy answers use server-filled factual placeholders too. This is an
exact template contract, with no multilingual natural-language parser.

General `RESPOND` replies may use natural model wording, subject to conservative
language, length, OTP and action-claim filters. The backend records
`response_language`, `wording_source` (`model` or `template`) and `fallback_reason`
in response metadata and audits. `model` means its wording/template was accepted;
the backend still supplies plan and order facts. The browser displays these fields.

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
| Wording validation | Empty, over-long, unsafe, wrong-language or unrecognized proposal wording falls back to a deterministic line |
| `PLAN_REJECTED` | An unchanged rejected plan cannot be reoffered as a new agreement |
| `CLARIFICATION_REQUESTED` | Clarification turns consume the existing clarification budget; terminal outcomes get terminal wording |
| OTP redaction | Recognized volunteered 4–8 digit or common spelled-out OTPs are redacted before model use, stored text and returned voice transcripts |

Invalid model output triggers **one repair call** with the validation error attached; a second failure degrades the turn rather than guessing.

Each customer turn selects English (`en-IN`), Devanagari Hindi (`hi-IN`) or Roman
Hinglish (`hi-en`) from transcript text using a script/token heuristic. This
selection drives deterministic replies, streaming and blocking TTS, and cache
keys. Spoken acknowledgement filler is disabled. The initial
greeting remains Hinglish because no customer turn is available yet. Short or
ambiguous mixed-language turns can be misclassified. Streaming STT requests
`codemix` format: synthetic provider checks preserved English confirmations as
English and Hindi confirmations as Devanagari. Human speech and voice quality
remain unverified. See [synthetic test review](SYNTHETIC_TEST_REVIEW.md).

The agent synthesizes the opening and substantive replies only. It does not warm
or play acknowledgement clips. The additive `ack_played` field remains false for
existing clients. `VOICE_TURN` retains separate first-substantive-audio timings.
Speech synthesis converts server-rendered date/time labels into natural spoken
Hindi or English; stored timestamps and displayed plan facts stay exact.
Empty or unavailable recognition resumes listening without a fabricated customer
turn. See the `CONVERSATION_REPAIR_REVIEW.md` notes in the author's working tree.

Any non-confirmation customer turn clears previous confirmation. An intervening
question or degraded reply requires a fresh plan offer before a later yes can
confirm it. The explicit-confirmation vocabulary and policy-at-commit checks are
unchanged. OTP redaction covers recognized forms, not every possible obfuscation;
the UI waits for backend-redacted text before displaying typed input.

### 4.5 Repository map

| File | Responsibility |
|---|---|
| `backend/app/main.py` | FastAPI app, all HTTP routes, static demo mount |
| `backend/app/realtime.py` | Voice WebSocket: VAD, barge-in, turn assembly, event emission |
| `backend/app/streaming_stt.py` | Per-connection Prisma streaming STT client (binary audio in, transcript out) |
| `backend/app/gnani.py` | Gnani REST client: batch STT + TTS |
| `backend/app/voice.py` | Speech orchestration: transcribe → plan → synthesise |
| `backend/app/planner.py` | LLM planner, prompt, guards, repair, normalisation |
| `backend/app/dialogue.py` | Clarification merging, source-grounded recipients and proposal normalisation |
| `backend/app/availability.py` | Server-owned delivery availability, date feasibility and synthetic slot expansion |
| `backend/app/language.py` | Language selection, localized wording, placeholder validation and OTP redaction |
| `backend/app/speech_text.py` | Natural spoken date/time rendering for TTS |
| `backend/app/policy.py` | Pure policy engine — the only component allowed to say "allowed" |
| `backend/app/state_machine.py` | Call states, legal transitions, terminal states |
| `backend/app/calls.py` | Call lifecycle, turns, proposals, commits, idempotency, audit |
| `backend/app/dialer.py` | Automatic browser scheduling, recovery, current skip state and change-only audit |
| `backend/app/cancellation.py` | Pending-order cancellation rules and dispatch-safe responses |
| `backend/app/catalog.py` | Server-owned SwiftCart product catalog |
| `backend/app/demo.py` | Synthetic checkout, readiness and bounded operator APIs |
| `backend/app/operator.py` | Operator review, rejection and policy-validated plan revisions |
| `backend/app/phone_control.py` | Phone consent and per-session dispatch controls |
| `backend/app/telephony.py`, `backend/app/phone_media.py`, `backend/app/phone_audio.py` | Gated Twilio dispatch, signed callbacks, phone audio/playback adaptation and consent |
| `backend/app/tools.py` | Tool names and per-state tool permissions |
| `backend/app/models.py` | SQLModel entities: Order, DeliverySlot, CallSession, Turn, Proposal, Decision, Commit, AuditEvent |
| `backend/app/schemas.py` | Pydantic request/response contracts |
| `backend/app/llm.py` | OpenAI-compatible and Gemini clients with strict JSON Schema mode, timeouts, token accounting |
| `backend/app/http_pool.py` | Shared HTTP client pool with proxy-bypass handling |
| `backend/app/seed.py` | Synthetic SwiftCart orders (1042–1049) covering every policy branch |
| `backend/app/config.py` | All settings, env-var driven |
| `backend/static/` | Legacy browser voice demo; the React app is served from `frontend/dist` when built |
| `frontend/` | React 19 + Vite dashboard, SwiftCart shop, voice studio and operator review |
| `evals/` | Deterministic scenarios, live planner cases, live voice matrix + reports |

### 4.6 Automatic browser scheduling

With `DIALER_ENABLED=true` (the default), startup performs an eligibility sweep
and starts one background task, repeating every five seconds. Shutdown stops and
awaits that task before closing clients. A worker failure appears in logs and in
`GET /api/dialer/status`; a later successful sweep clears its error.

Automatic candidates may be `PENDING`, `RESCHEDULED` or `OUT_FOR_DELIVERY`,
have complete metadata and no opt-out, and have a prior failed delivery, COD payment, OTP requirement or value
of at least INR 25,000. Their window must satisfy
`window_start - 60 minutes <= now < window_end`. Naive order timestamps follow
policy's IST interpretation; stored call timestamps without a zone use UTC.

Manual starts and sweeps share admission checks and a process lock. All call
session rows count toward a maximum of three attempts per order. A retry waits
one hour from the latest attempt's end (or start if no end is recorded), also
respecting `last_call_completed_at`. `prior_attempts` remains the count of failed
deliveries. Committed, escalated and turn-budget-exhausted history blocks retries.
`NO_ANSWER` and `UNREACHABLE` can retry after eligibility/cooldown checks;
`TOOL_FAILURE` needs an explicitly recorded transient disposition. Unknown failures
require human review. One active demo call reserves capacity across all orders.

The default transport is **browser**. A `STARTED` event means a local
attempt/session was created, with no phone or provider acceptance implied. The
existing order button attaches to that session, completes connection/context
steps as necessary, and checks their responses before opening voice.

Unanswered browser attempts expire after 120 seconds. Disconnect keeps a
30-second reconnect grace; an abandoned connected session without a live socket
expires after 300 seconds without activity. Live socket ownership prevents
abandonment expiry. These are configurable via `DIALER_WAITING_TIMEOUT_SECONDS`,
`DIALER_DISCONNECT_GRACE_SECONDS` and `DIALER_IDLE_TIMEOUT_SECONDS`. Recovery retains
attempt history and cannot overwrite a concurrent answer or commit. The timing
tests use an injected clock; recovery runs on the next sweep after a deadline.

`DialerEvent` records `STARTED`, observed `DISPOSITION_CHANGED` and `SKIP_CHANGED`
events. Unchanged sweeps do not append duplicates. `DialerOrderState` separately
holds the last evaluated skip reason, session link and `checked_at`, exposed by
the additive read-only `/api/dialer/status` endpoint. Existing session state audits
remain intact. Three new tables are created through the existing `create_all`
startup path; existing table columns are unchanged.

Run **one application process/worker**. Locks and socket ownership are local to
that process; this implementation does not provide distributed scheduling.
Fixed seed windows are unchanged, so they are eligible only near their documented dates.
Set `DIALER_ENABLED=false` to stop automatic scheduling and timeout recovery.

### 4.7 SwiftCart shop and operator dashboard

`/` is the React landing page; `/dashboard` is the operator dashboard. `/shop`
creates fictional SwiftCart deliveries; `/voice` is the customer voice client
(the original `/index.html` remains as a legacy fallback page).
Checkout accepts a server-owned catalog product and payment mode. OTP verification
is derived from payment: prepaid requires it, COD does not. Catalog price and
synthetic delivery details are controlled by the server. No real payment or
customer contact/address is collected. New orders start PENDING with delivery
tomorrow, 10 am to noon IST. Delivery availability spans 8 am to 6 pm IST on that date and the following date; appointments can fit anywhere inside those periods. Checkout and
delivery times are separate. The readiness action simulates OUT_FOR_DELIVERY;
it is idempotent and cannot rewrite historical seed orders. Automatic dispatch
still respects the delivery-window horizon. The explicit PC-demo button can
start a rehearsal earlier. Explicit cancellation changes only a PENDING order;
dispatched orders receive an explanation instead of a false cancellation.

Additive APIs:

| Endpoint | Contract |
|---|---|
| `POST /api/orders` | Synthetic checkout choices only; unknown/personal fields are rejected |
| `POST /api/orders/{id}/out-for-delivery` | Deliberately mark a checkout order ready |
| `GET /api/sessions?active=true&order_id=1050&limit=50` | Bounded summaries, proposal/verdict, confirmation, transport and courier card; limit at most 100 |
| `GET /api/sessions/{id}/turns?after_id=0&limit=100` | Ordered transcript and planner metadata, redacted at read time too; limit at most 200 |
| `GET /api/dialer/events?order_id=1050&limit=50` | Latest bounded order events; limit at most 100 |

The dashboard polls every two seconds and shows active/completed calls,
transcripts, response language, wording source/fallback reason, policy results,
confirmation, courier cards, scheduler reasons and audits. Untrusted text is
rendered through `textContent`. Refresh failures mark displayed data as possibly
stale. These local demo pages/APIs have no added authentication and are not a
public deployment.

### 4.8 Gated phone adapter: locally tested, live validation pending

The adapter uses existing `httpx` and standard-library audio/signature code; no
Twilio SDK or codec dependency was installed. It reuses the browser voice engine
for VAD, cancellation, planning and TTS orchestration. Native 8 kHz mu-law Timbre
requests are adapted to PCM16 for that engine, and outgoing audio is encoded as
raw mu-law for Twilio. Streaming and blocking fallback carry the turn language.
Media framing, playback marks and clear-on-interruption are tested with mocks.
Twilio specifies this audio format and playback protocol in its
[Media Streams reference](https://www.twilio.com/docs/voice/media-streams/websocket-messages).

`TELEPHONY_PROVIDER=none` retains browser-only operation. Phone dispatch also
requires configured credentials, a public HTTPS endpoint, a verified console
test, confirmed streaming access, Gnani permission and approval for one exact
existing local session. These gates default closed:

```dotenv
TWILIO_CONSOLE_VERIFIED=false
TWILIO_STREAMING_VERIFIED=false
GNANI_PHONE_DEMO_APPROVED=false
TWILIO_APPROVED_SESSION_ID=
```

Setting credentials alone does not authorize a call. After approval immediately
before a live test, only the approved CALLING session can be dispatched by the
sweep; a retry or another order needs a new session-specific approval. The only
recipient is the handset read from local Settings. No dial endpoint accepts an
arbitrary recipient. Keep number, credentials and public URL in local `.env`.
There is no call recording request or audio-file persistence.

The answer webhook asks for DTMF consent to speak. Pressing 1 starts the media
stream; it does not confirm a delivery plan. Refusal or ambiguous consent ends
with human-review disposition. HTTP callbacks validate signatures over all form
parameters. The WebSocket validates its signed public WSS URL, account/call/stream
binding, format and consent. Callback sequences and terminal fences reject
replays/regressions. The signature tests include an
[official SDK duplicate-parameter vector](https://github.com/twilio/twilio-python/blob/main/tests/unit/test_request_validator.py).
The validator is a deliberately narrow form/WSS implementation; exact configured
URLs must match. It has not been verified through a real proxy/provider handshake.

An accepted, requesting or ambiguous provider call keeps its capacity reservation
until a provider outcome or explicit reconciliation resolves it. Ambiguous POST
timeouts and server errors are not blindly retried. This can deliberately block
the demo if callbacks are missing. Only an explicit 429 rejection is recorded as
a transient dispatch failure. Provider error bodies, phone numbers and secret
URLs are excluded from audit payloads and dashboard summaries.

The dashboard distinguishes phone consent, dispatch state and delivery-plan
confirmation. A phone turn commits only through the same explicit-confirmation
and policy-revalidation path as browser calling. A completed provider callback
cannot overwrite a committed plan. Before any live test, obtain Gnani permission,
verify the console call/account's streaming access, expose only `/twilio/*` through
an approved HTTPS/WSS ingress, and obtain immediate approval to dial. No tunnel
or public deployment was created in this pass. Twilio's current trial blocks
`<Stream>`; basic trial minutes alone do not enable this bridge. See
[Twilio trial restrictions](https://www.twilio.com/docs/usage/trials/try-out-voice#blocked-verbs).

The live phone flow, handset audio quality, carrier delivery and proxy signature
behavior remain unmeasured. Native 8 kHz audio and the adapter have since been
exercised locally with real speech providers and mocked Twilio transport; this
does not establish handset performance. Browser visual QA was also
unavailable because the computer-use host was not connected; DOM and HTTP tests
are reported separately in `D_REVIEW.md` and `E_REVIEW.md`.

---

## 5. Test & evaluation suites

| Suite | Command | Network | Proves |
|---|---|---|---|
| Unit + integration | `cd backend && uv run pytest` | none | Policy, state machine, planner guards, scheduler, synthetic shop/dashboard, VAD, streaming STT and mocked phone transport — **698 tests** |
| Browser regressions | `cd backend && node --test --test-reporter=tap tests/voice_browser.test.cjs tests/demo_browser.test.cjs` | none | **24 tests** of voice, shop and dashboard behavior against browser API/DOM fakes |
| Frontend unit | `cd frontend && npm test` | none | **40 Vitest tests** for delivery progress, voice controller and selection state |
| Frontend e2e | `cd frontend && npm run test:e2e` | none | **183 Playwright checks** (61 specs × desktop/mobile/tablet) for landing, shop, dashboard, voice and operator flows with mocked API, audio and WebSocket |
| Frontend build | `cd frontend && npm run build` | none | TypeScript and Vite production build served by FastAPI |
| Deterministic scenarios | `cd backend && uv run python ../evals/runner.py` | none | End-to-end call flows with scripted plans and no LLM |
| Live planner cases | `cd backend && uv run python ../evals/planner_runner.py` | Configured planner | The model returns schema-valid decisions for fixed Hinglish turns |
| Live voice matrix | `cd backend && uv run python ../evals/matrix_runner.py` | Gnani + configured planner | The real WebSocket voice path, policy, commit and audit trail, T01–T22 |

### Earlier local acceptance on 12 November 2026

The complete backend suite passed 677 tests. Vitest passed 40 tests, Node passed
24 regressions, deterministic scenarios passed 12/12, and the frontend build and
Ruff checks passed. The full Edge browser run passed 167/168 checks. One tablet
page failed to load its assets with `ERR_NETWORK_CHANGED`; that case then passed
three consecutive isolated reruns. On this Windows host, Vite and Vitest used
`--configLoader runner`, and Playwright used `PLAYWRIGHT_CHANNEL=msedge`.

The local environment resolved to Gemini `gemini-3.1-flash-lite`. Two isolated
browser conversations exercised real Gemini planning and Gnani speech output
with typed customer input. A Hindi neighbour request retained the supplied name
and committed after natural Hindi confirmation. A second conversation rejected
an unavailable date, interpreted five o'clock as 17:00–18:00, revised the date
after a qualified acknowledgement, and saved only after fresh confirmation.
Each session produced one delivery commit. Courier instructions retained direct
courier-to-customer OTP verification.

The initial greeting failed after 33.92 seconds while recognition connected
eagerly. After deferring recognition connection until capture, a fresh greeting
streamed its first audio at 2.098 seconds. Model-backed reschedule replies started
audio at 4.546 and 5.331 seconds. These are individual server measurements.
Microphone recognition, acoustic quality, interruptions and handset calling still
require live acceptance. The synthetic sessions used a disposable database with
telephony disabled.

### Live voice matrix

The matrix drives the actual server over HTTP and WebSocket: it synthesises every customer turn with Gnani Timbre, streams it as 16 kHz PCM over the voice socket, reads planner and policy decisions from SQLite, commits through the real endpoint, and writes a report.

**Historical NVIDIA NIM run: 19 / 21 live scenarios pass.** These figures do not
describe the current OpenRouter configuration. Current provider and microphone
acceptance remain pending.

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

The two failing scenarios (T01, T04) failed because their first planner call hit
the 12 s provider timeout and the safe fallback consumed the turn — the
degradation is counted in the report rather than hidden. They were **not**
re-run on a healthy provider, so no claim is made that they pass.

Full report: [`evals/reports/matrix-latest.md`](evals/reports/matrix-latest.md) · raw JSON with per-turn transcripts, policy verdicts, timings and full audit trails in `evals/reports/matrix-latest.json`.

### Synthetic provider checks after the repairs

On 31 October 2026, additional isolated tests exercised real Gnani speech and
NVIDIA NIM, including English/Hindi confirmations, COD refusal and correction,
greetings, OTP refusal, factual questions and native 8 kHz audio. Twilio HTTP,
playback acknowledgements and the customer were simulated. Results, fixes,
commands and timing limits are in [the review](SYNTHETIC_TEST_REVIEW.md). These
checks do not replace or rerun the historical 21-case matrix above.

---

## 6. Problems we hit, and how we fixed them

These are the real engineering problems from building this, not a retrospective of easy parts.

**1. The provider stalls, and a stall is not a bug in your code.** The same planner prompt measured 0.7 s to 6.9 s across runs, and intermittently exceeded any sane deadline. We bounded it (12 s during the matrix run below, 30 s after earlier hosted providers were measured up to 9.4 s), made the failure mode a *safe spoken fallback* instead of silence, and made the evaluation report count degraded turns explicitly (`Degraded planner turns: 3 (LLM_TIMEOUT: 3)`) rather than letting a provider hiccup masquerade as a logic defect.

**2. Devanagari nukta is not one codepoint.** Prisma returned `ढ़` as the precomposed `U+095D`, while our confirmation whitelist was written for `ढ` + `U+093C`. The live transcript was correct; our normalisation was not, so a legitimate "हाँ ठीक है आगे बढ़ाइए" was rejected as a non-confirmation. Fix: fold precomposed nukta to decomposed form *before* token mapping.

**3. Names are stored in the script the customer used.** We do not transliterate: the plan stores `शर्मा जी`, and the courier card says `Leave with neighbour: शर्मा जी`. Our own evaluation initially failed on that, which is the correct outcome — a test asserting the wrong script is a bug in the test.

**4. "Hand it to me" vs "reschedule" was ambiguous.** The planner sometimes emitted `HAND_TO_CUSTOMER` with a *different* window, which policy then rejected as an unavailable slot — technically valid, conversationally nonsense. We normalised the invariant: `HAND_TO_CUSTOMER` means the order's own current window, and every other window is `RESCHEDULE`. The rewrite is audited as `PLAN_NORMALIZED`.

**5. An order's own window is not always in `available_slots`.** The order's scheduled window is already deliverable, so the policy now accepts a window that equals the order's window *or* matches a slot row. Before this, a perfectly valid handoff looked illegal on orders whose slots only listed future alternatives.

**6. Refusals were explained in the wrong order.** Telling a customer "that time window is not available" when the real blocker was *COD parcels cannot be left with anyone* is technically true and completely useless. Violation messages are now ordered by severity: hard constraints (escalation, OTP, COD, high value, security, neighbour) are explained before slot availability.

**7. A bare "haan" must not commit anything.** Confirmation is only honoured when the last agent turn actually offered a plan (`PROPOSE_PLAN` / `CONFIRM_PLAN`). After a clarification or a degraded turn, "haan" produces `CONFIRMATION_WITHOUT_OFFER` — the call stays in `PENDING_CONFIRMATION` and nothing is committed.

**8. The customer can ask us to break the rules, and sometimes asks politely.** "ignore the rules" is matched deterministically (English and Devanagari variants, with filler words), escalated immediately without an LLM call, and written to the audit trail as `POLICY_OVERRIDE`.

**9. Streaming STT state leaked between calls.** A new voice connection inherited the previous connection's queue, partials and error state. Each connection now gets its own client with an explicit reset, so a stale sentinel cannot poison the next call.

**10. Our own harness can produce false failures.** One published-looking run failed T08 because it was launched from a branch that predated the scenario fix — the product was right, the expectation was stale. Lesson applied: published matrices should be run from a known, documented commit. The currently published report was **not** run from merged `main`, and says so.

---

## 7. Getting it running

### Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- A Gnani API key (Prisma + Timbre), a Gemini API key and a local operator token

### 1. Configure

```bash
cd backend
cp .env.example .env
```

Set the speech/planner credentials and planner connection in `.env`:

```dotenv
GNANI_API_KEY=
LLM_PROVIDER=gemini
GEMINI_API_KEY=
OPERATOR_API_TOKEN=
# Empty base URL and model select the Gemini defaults.
LLM_BASE_URL=
LLM_MODEL=
LLM_THINKING_LEVEL=minimal
LLM_TEMPERATURE=1.0
LLM_MAX_TOKENS=1024
LLM_STRUCTURED_MODE=json_schema
```

The default planner uses native Gemini with `gemini-3.1-flash-lite`.
Set `GEMINI_API_KEY` in the local `backend/.env`, with `LLM_PROVIDER=gemini`.
Empty `LLM_MODEL` and `LLM_BASE_URL` select the provider defaults. The backend
validates structured output, source evidence, delivery policy and explicit
customer confirmation before saving instructions.

Optional adapters for OpenRouter, Groq and Ollama remain selectable. Each reads
its own credential. HTTP 429, 402 and timeout responses have distinct recorded
error categories and visible frontend feedback. Provider errors preserve the
saved delivery arrangement. Gnani handles speech recognition and synthesis.

`.env` is gitignored. Keys are never committed and never printed.

The historical synthetic checks used one primary Gnani key for speech and one NIM key for
planning; extra Gnani keys were not required for these sequential conversations.
Account concurrency and quota remain provider-controlled. Adding keys does not
by itself prove that more concurrent calls are permitted.

### 2. Install and run

```bash
cd backend
uv sync
uv run uvicorn app.main:app --port 8000
```

Open `/shop` for the customer storefront and `/dashboard` for delivery
operations in two deliberately opened tabs. Follow the deadline demo sequence
below. The operator console owns dispatch and conversation preparation.

The database is created and seeded automatically on startup (8 synthetic orders, ids `1042`–`1049`, covering OTP, COD, high-value, neighbour-drop, missing-metadata and already-delivered cases).

### 3. Run the tests

```bash
cd backend
uv run pytest              # 698 tests, no external network required
node --test --test-reporter=tap tests/voice_browser.test.cjs tests/demo_browser.test.cjs  # 24 browser regressions

cd ../frontend
npm ci
npm test                   # 40 frontend unit tests
npm run test:e2e           # 183 Playwright checks (mocked API)
npm run build              # production build served by FastAPI
```

### 4. Run the evaluations

```bash
# deterministic scenarios, no network
cd backend && uv run python ../evals/runner.py

# live planner cases (configured planner)
cd backend && uv run python ../evals/planner_runner.py

# live voice matrix (Gnani + configured planner) — use a disposable database
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
| Text-to-speech | **Timbre v3.0** (`POST /api/v1/tts/inference`) | Voice `Shivani`, per-turn `en-IN`, `hi-IN` or `hi-en` |
| Planner | **Gemini** (`gemini-3.1-flash-lite`) | Native structured output with server-side source, policy and confirmation validation. Optional OpenRouter, Groq and Ollama adapters remain available. |
| Backend | FastAPI, SQLModel (SQLAlchemy) on SQLite | WebSocket voice path, no external database service |
| Evaluation | Configured planner + Gnani Timbre + Prisma speech | Historical 21-case matrix used NIM; the 12 November video-demo replay uses native Gemini |

Built directly on the Gnani **API platform** (`app.gnani.ai/voice`) — Prisma and Timbre are called from our own service code. No hosted agent orchestrator owns the conversation.

---

## 9. What is real, and what is not

Honesty about this is deliberate — the evaluation claims only hold if the boundary is explicit.

| Real | Simulated / out of scope |
|---|---|
| Streaming Prisma STT over a live WebSocket, real barge-in and VAD | The customer is **synthesised** with Gnani Timbre — no human speaker, no telephone codec, no carrier jitter |
| Timbre TTS synthesising every agent reply | White Gaussian noise at 8 dB SNR stands in for recorded street audio |
| Configured planner producing the structured plan when available | Orders, customers and the SwiftCart merchant are **synthetic** |
| Real policy evaluation, state machine, commit, SQLite writes, audit trail | Twilio adapter is gated and tested with mocks; no real handset call was exercised |
| Real commit-time revalidation and idempotency | Commit is a local revalidation + SQLite write, not a remote tool — so T19's failure semantics are covered by tests instead of a live timeout |
| Real Prisma/Timbre calls and the historical NIM planner in the live matrix | Prisma streaming STT emits **no confidence score**, so the live path relies on the explicit-confirmation gate; low-confidence rejection is covered by deterministic tests |

No real customer data, no real phone numbers, no OTP collection, and no impersonation of any real delivery company.

---

## 10. Known limits

- **Provider-specific validation.** The 12 November isolated acceptance used Gemini `gemini-3.1-flash-lite` and Gnani output with typed customer input. Gemini is the sample default. Historical Groq and NIM results describe their recorded configurations. Headset audio and telephony still require validation.
- **Ollama Cloud is paid.** It remains selectable, but a key without an active paid plan returns HTTP 402; the planner reports `LLM_BILLING_REQUIRED` and preserves the delivery state. Gemini is the default.
- **Latency remains open.** The historical live matrix measured P50 3.87 s / P95 13.47 s end-to-end. The additional synthetic checks measured `first_audio_ms` with real providers, with several responses taking multiple seconds. These measurements exclude endpoint detection, device playback and carrier delay; spoken acknowledgements are now disabled. The target remains sub-second time to first audio.
- **A1/A2 validation is synthetic.** Detection now calibrates, retains onset audio and debounces barge-in, but energy alone cannot distinguish speech from every sudden noise or speaker echo. Keep quiet briefly during initial calibration. Quiet barge-in before calibration uses a provisional threshold and may be missed. Real provider tests now cover the repaired path, but real microphone and acoustic echo performance remain unmeasured; the matrix above is historical.
- **Streaming STT gives no confidence**, so the live path cannot reject a low-confidence transcript by score; it compensates with the explicit-confirmation gate.
- **Planner stalls are external.** The 30 s bound is a product trade-off (silence on a call is worse than a safe re-ask), not a fix for the provider.
- **Single-order scope.** No multi-parcel routing, no courier-side app, no dispatch integration.
- **Evaluation scale.** 21 live scenarios, one planner model, one voice, one language mix. Changing the model, prompt, VAD thresholds or TTS settings invalidates comparisons with older reports.

## 11. What's next

- **Latency programme** — sub-second time-to-first-audio: overlapped speech-first planning, chunked TTS, tighter endpointing, a pre-synthesised fallback voice, and a smaller/faster planner model.
- **Live phone validation** — an approved owner-handset test after account, ingress and challenge-permission checks.
- **Public deployment** — a hosted instance so judges can try it, with credits-aware guardrails.
- **Broader language coverage** and a telephony adapter.

---

## 12. Repository

```
pahunch/
├── backend/
│   ├── app/            # FastAPI service, planner, policy, voice and operator APIs
│   ├── static/         # legacy browser voice demo
│   ├── tests/          # Python integration tests and Node browser-audio regressions
│   └── scripts/        # realtime smoke test, STT latency spike
├── frontend/
│   ├── src/            # React 19 dashboard, SwiftCart shop, voice studio and operator console
│   ├── tests/          # Vitest unit tests and Playwright acceptance/visual checks
│   └── dist/           # production build served by FastAPI when present
├── evals/
│   ├── scenarios.json          # deterministic call scenarios
│   ├── planner_cases.json      # live planner cases
│   ├── matrix_scenarios.json   # live voice matrix T01–T22
│   ├── runner.py               # deterministic scenario runner
│   ├── planner_runner.py       # live planner runner
│   ├── matrix_runner.py        # live voice matrix runner
│   └── reports/                # published matrix reports
└── README.md
```

There is no `.github/` directory and no committed CI configuration.

Built with [FastAPI](https://fastapi.tiangolo.com/), [SQLModel](https://sqlmodel.tiangolo.com/), [httpx](https://www.python-httpx.org/), [uv](https://docs.astral.sh/uv/), and the [Gnani AI](https://app.gnani.ai/voice) API platform.

## License

MIT — see [LICENSE](LICENSE).

## React frontend

The React/Vite application lives in `frontend/` and uses the existing backend
APIs and voice WebSocket. Keep provider credentials in `backend/.env`; they are
not needed in the frontend environment or browser bundle.

For frontend development, run the backend on `http://127.0.0.1:8000`, then:

```bash
cd frontend
npm ci
npm run dev
```

Open the Vite URL printed in the terminal. Its development proxy forwards
`/api`, `/health`, `/ws` and the audio worklets to the local backend.

Run the frontend unit tests, browser checks and production build with:

```bash
cd frontend
npm test
npm run test:e2e
npm run build
```

For one server hosting the built frontend and backend:

```bash
cd frontend
npm run build
cd ../backend
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Build before starting the backend, or restart it after the first frontend
build. When `frontend/dist/index.html` exists at startup, the backend serves
the React app at `/`, `/dashboard`, `/shop` and `/voice`, with compiled assets
under `/assets`. Existing root links containing `order_id` redirect to
`/dashboard` with their query string preserved.

Without a frontend build, these pages use the original local HTML interfaces.
The original `/index.html`, `/dashboard.html`, `/shop.html`, `/worklet.js` and
`/pcm-player.js` remain available with either configuration. Unknown API and
page paths retain their 404 responses rather than loading the React app.


## Current deadline demo — SwiftCart and Pahunch

Use the installed local environment from Windows CMD.

```bat
cd /d "C:\Users\sweta\Desktop\Gnani AI\pahunch\frontend"
npm.cmd run build -- --configLoader runner
cd ..\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

1. Open `http://127.0.0.1:8000/shop` as the customer. Select a product and
   payment method, then place the demo order. Keep its order page open.
2. Open `http://127.0.0.1:8000/dashboard` as the operator. Enter the existing
   `OPERATOR_API_TOKEN` from the local `backend/.env` in the access form.
   Select the order and choose **Call customer** to confirm availability before dispatch.
3. Return to the customer tab and accept the incoming-call prompt. The dedicated
   call page opens and requests microphone access. Keyboard fallback is available
   for scripted rehearsal.
4. Discuss the arrangement and explicitly confirm the final version. In the
   console, open **Courier instructions** to inspect the saved commit, original
   address, authorized recipient, handoff location, time and OTP instructions.

The landing page and storefront use white, yellow and charcoal.
The operations console uses near-black, blue, green and gray. Dispatch actions
require operator access. Pending orders can receive availability calls before
the courier departs. **Mark out for delivery** separately records dispatch.
The console initially lists the 20 most recent catalog orders; history includes
older records, and the selected order stays selected as new orders arrive.

Neighbour handoffs require a name and a house/flat number or clear finding
instructions. Bare proximity such as “बगल में” or “next door” prompts a location
question. A replacement neighbour requires its own location and fresh
confirmation. COD and high-value restrictions are checked before collecting an
ineligible neighbour's location. Courier-to-customer OTP verification applies
at handoff. The AI never collects the code.

Commit updates the saved courier arrangement and any changed appointment
atomically. A pending parcel stays pending after a confirmed appointment change.
The original delivery address remains the address of record. An already-dispatched
parcel can receive a revised appointment on the same order. Physical route
adjustments remain the courier operation’s responsibility.
The courier view displays the authorized neighbour location separately and
reports unavailable contact details explicitly. Refresh failures retain the
last retrieved instructions with a stale-data warning.

### Video-demo repair verification

The backend suite passed **696 tests**, frontend unit checks passed **40**, Node
browser/audio regressions passed **24**, deterministic scenarios passed **12/12**,
and the responsive mocked browser suite passed **171 checks**. Build and static
checks accompany these runs. Mocked tests cover role separation, protected
dispatch, recipient correction, one commit, refresh recovery and accessibility.
Real-provider rehearsal records are stored locally under ignored `.artifacts/`.
A clean seven-turn replay passed against Gemini `gemini-3.1-flash-lite` and
Gnani speech output using scripted customer input. It rejected vague proximity,
requested a replacement neighbour's location, retained house 404 through time
and date changes, and saved one idempotent commit. The order and courier card
represent the same final appointment, with the original address retained.
First substantive audio for model-backed turns ranged from **1.95–3.27 seconds**;
the explicit-confirmation reply started at **250 ms**. Opening audio started at
**1.924 seconds**. These server measurements exclude microphone endpointing and
device playback. The replay recorded zero planner errors.
Microphone acoustics, real interruption quality and handset delivery remain
acceptance work. The approved courier delivery mechanism is the live console
view. Telephony remains deferred.

Operator rejection and revision require a reason and the current proposal
version. Revisions require presentation and fresh customer consent. The operator
key is held in that browser tab's session storage.

### Delivery availability and rescheduling

Availability periods come from the backend, independently of the initial two-hour
appointment. A 3 pm request within a recorded 10 am–6 pm period can become a 3–5 pm proposal.
A start-only request at 5 pm can use 5–6 pm; explicit endpoints are never silently
shortened. Requests outside recorded days/hours are rejected with actual available
periods, and availability questions bypass the model. Changes still require fresh
customer confirmation before commit. Committing RESCHEDULE updates both the order
appointment and courier card.

Synthetic checkout currently assumes earliest delivery tomorrow; no warehouse,
origin, carrier ETA or live route-capacity feed is connected. Those dates must come
from fulfillment data when integrated. Existing unresolved synthetic checkout
availability is widened on startup without changing its scheduled appointment or
earliest stored date. Legacy seeded examples retain their recorded availability.

### Conversation recovery

The current default planner is Gemini `gemini-3.1-flash-lite`. Short acknowledgements preserve the conversation language;
substantial Hindi/English mixing selects Hinglish. Server-known delivery windows
are accepted in both 24-hour and 12-hour forms (`18:00` and `6 PM`). Model
replies may use up to three sentences, matching the server's own availability
reply; four sentences still fall back to deterministic wording. The planner can
mark an unfinished clarification with `awaiting_continuation`; voice output then
waits up to two extra seconds, cancellable by resumed speech, without a second
model request.
Complete delivery actions cannot use this flag. Noise calibration and browser
noise suppression help capture but do not guarantee speech/noise separation.
Human microphone validation is still required.

OTP neighbour delivery is permitted only for an eligible, named recipient with
an arrival call. The courier must contact the customer and verify OTP directly
before handoff. The assistant never collects or verifies the OTP. No contact or
failed verification means no handoff. New delivery windows must fit recorded
availability within 10:00–18:00 IST; legacy scheduled promises are preserved.

### Source-backed conversation recovery

Recipient names and house details can carry exact customer-source quotes and turn
references. The backend inserts the verified original spelling into proposals and
courier instructions. Saved proposal JSON from earlier versions remains readable.
Typed request details retain understood preferences between turns, including a
start time without an end. Within recorded availability, a five PM start is capped
at the six PM closing boundary. Repeated clarification marks a recoverable review
need instead of closing the session. Explicit human requests and the overall turn
budget still apply. Ordinary Hindi/Hinglish variation is recorded as a language
warning while explicit language requests remain authoritative.
