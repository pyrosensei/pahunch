# Codex task: repair Pahunch's voice backend, then complete the phone demo

You are working in an existing repository. Read these before doing anything else,
in this order:

1. `AGENTS.md` — binding rules for this repository.
2. `README.md` — architecture, setup, evaluation and known limits.
3. `CODEX_PROMPT.md` — this file.
4. `../claude-handoff-prompt.md` — the earlier handoff, if it exists.

Then **analyse the codebase yourself before writing any code.** Your first
deliverable is the written analysis in section 7, not an implementation. Verify
every referenced function against the current tree; do not trust old line numbers.

Do not rebuild completed work. Do not treat an investigation or a proposed design
as an implemented feature.

---

## 1. Product and non-negotiable thesis

Pahunch is a policy-bound voice agent. It calls a customer before a high-risk
delivery (COD, OTP-required, high-value, or a previous failed attempt), collects a
conditional handoff instruction in English, Hindi or code-mixed Hinglish,
validates it against courier policy, negotiates a permitted alternative, and
commits an executable plan with a courier card and audit trail.

The core claim: **the LLM proposes, deterministic policy code disposes.** The model
never decides what is permitted, and no spoken reply may promise something the
validated plan does not contain.

Built for the Gnani AI Great Indian AI Internship Challenge 2026. Submission
deadline: **2026-11-10 23:59 IST**. The owner needs a reliable, demonstrable
product, not a larger architecture.

Providers: **Gnani Prisma STT and Timbre TTS**, **NVIDIA NIM** for the planner.
Backend: FastAPI + SQLModel + SQLite. Current demo frontend: static HTML/JS served
by the same app. The suite was last verified at **149 passing tests**; rerun it to
establish your own baseline.

---

## 2. Current repository state and completed work

Foundation already implemented:

- Synthetic orders, delivery slots, call sessions and seed data.
- Deterministic policy engine with typed violations and allowed alternatives.
- Call state machine, explicit confirmation, policy revalidation at commit,
  idempotent commit, courier card and audit records.
- Structured planner output with schema validation and a repair path.
- Deterministic safety replies for OTP refusal, human/robot questions and prompt
  injection.
- Live T01–T22 matrix harness with JSON and Markdown reports.

Latency and browser-demo work already implemented:

- `backend/app/http_pool.py`: one keep-alive HTTP client per event loop.
- `backend/app/gnani.py`: request serialization, key rotation, 429 retry/backoff,
  streamed raw PCM16 TTS and blocking fallback.
- `backend/app/voice.py`: acknowledgement audio when the planner exceeds 550 ms.
- `backend/app/planner.py`: explicit JSON output shape to prevent whitespace
  padding, truncation and a second repair call.
- `backend/app/realtime.py`: greeting, microphone hold during greeting, secondary
  endpointing threshold, greeting barge-in and `scripted_turn` demo input.
- `backend/static/index.html`: AudioWorklet PCM playback at the browser rate,
  active-call resume, typed fallback and visible microphone errors.
- `backend/static/worklet.js`: 20 ms microphone frames.
- `backend/scripts/gnani_key_probe.py`: live concurrency/latency probe that never
  prints key material.
- `backend/scripts/telephony_audio_probe.py`: phone-format speech compatibility
  probe.

Git: 11 small commits exist on `feat/voice-latency-and-demo`; they have not been
merged or pushed. Check `git status` and `git log --oneline main..HEAD` yourself.

**Important:** the later Claude session primarily investigated and made decisions.
It did **not** implement the dialer, conversational repair, VAD repair, dashboard,
shop or Twilio bridge. Twilio settings in `backend/app/config.py` and
`backend/.env.example` are placeholders with no telephony client behind them.

---

## 3. Product decisions made after the original handoff

These replace the corresponding open questions in earlier briefs:

1. **The desired customer experience is a real phone call to the owner's own
   verified test handset.** Twilio trial + Media Streams is the preferred path.
   No calls to real customers or third parties. Ask immediately before any live
   call test, as required by `AGENTS.md`.
2. **The browser is an operator dashboard, not the primary phone.** Keep browser
   calling as a development and demo fallback.
3. **Add a small fictional SwiftCart shop** so the owner can create a synthetic
   order, choose COD/prepaid, and move it to out-for-delivery. A demo delivery
   window starting about 15 minutes after order creation should make window
   gating observable without waiting hours.
4. **Delivery-window gating is ON.** The proposed demo rule is an eligible
   out-for-delivery order whose delivery window is within the next 60 minutes;
   verify the existing model and recommend the smallest correct implementation.
5. **Retry policy:** wait **1 hour between attempts**, with at most **3 calls**.
   Do not auto-retry committed, escalated or turn-budget-exhausted calls. Retry
   no-answer, unreachable and suitable transient tool failures only when policy
   and cooldown allow it.
6. **Language mirroring is required:** answer in English when the customer speaks
   English, Hindi in Devanagari for Hindi, and Roman-script Hinglish for Hinglish.
   Do not always force Hinglish.
7. Use an **in-process dialer** by default unless your analysis finds a concrete
   reason it cannot preserve correctness. Do not introduce a separate worker
   system without approval.
8. Extra Gnani keys are **not required for one phone call**. Do not make key
   pooling a prerequisite for the demo.

Privacy and contest gate:

- The owner's handset number and all Twilio credentials belong only in local
  `.env`; mask the number in UI/logs and never commit or print it.
- The challenge FAQ reportedly prohibits real phone numbers and recorded calls in
  the project/demo. Calling the owner's own phone is a grey area until Gnani
  confirms it. Keep the browser fallback and do not publish a real-phone recording
  until that permission is explicit.
- Twilio account setup and cloudflared installation were started in the earlier
  session, but completion and call delivery are not verified.

Target demo story: **SwiftCart order → eligible order is discovered → owner's
phone rings → agent negotiates and mirrors language → operator dashboard shows
transcript, planner wording source, policy verdict, dialer reason, courier card
and audit trail.**

---

## 4. Original owner report — the failures to fix

> "the product is not performing well [...] in my real test use case, i was
> expected to choose an order and then start a call after that, it all dosen't
> work like that, i should get a call automatically if in the orders folder my
> order is passing the creteria for call that i actually had to"

> "for calling it was taking 10-25 seconds to detect my voice and change and the
> only response it was giving is
>
> 'Theek hai — direct handoff, 05 Nov 18:00-20:00 IST. Kya main is plan ko aage
> badhaun?'
>
> this is the only thing it spoke, so where is the llm even there? it feels like
> it's automated response rather than an llm, it wasn't responding to whatever i
> was saying, and it didn't even said anythig else, just this thing, also the ui
> is shitty but first i gotta work on backend"

The UI complaint is real, but backend correctness comes first. The later visual
polish pass is a separate task.

---

## 5. Existing findings: verify, then use

Distinguish confirmed code/audit evidence from inference. Do not spend the budget
rediscovering these, but correct anything the current tree disproves.

### 5.1 Confirmed: the model's words are discarded on plan proposals

`backend/app/planner.py` defines `plan_result_message()` and uses its template for
`PROPOSE_PLAN` in `_execute_decision()`, discarding `decision.message`. The owner
heard that f-string verbatim. Preserve policy-safe wording when repairing this;
the deterministic template remains a valid fallback.

Audit evidence also showed the planner choosing `PROPOSE_PLAN` for greetings and
questions such as "helo" and "Kya aapko meri aawaz aa rhi hai". Check whether the
planner lacks an appropriate answer/clarify/small-talk action. Fixing only the
template will not fix a wrong action.

### 5.2 Confirmed: brittle VAD and poor missed-detection feedback

The realtime path uses a fixed int16 RMS threshold (`500` in current config), with
no noise-floor calibration or automatic gain. Quiet speech can be missed; a noisy
microphone can remain above threshold until the 15-second utterance cap. The
client also gates streaming on server listening state, and missed detection has no
useful level feedback.

The 15-second-cap explanation for the reported 10–25 seconds is **an inference**,
not something the audit proves. Measure before and after changing VAD.

### 5.3 Strong code-based finding: blocked turn-taking and stale audio

The realtime receive loop awaits utterance processing and audio delivery. While it
is busy, microphone frames can queue and later be replayed as stale audio.
Single-frame energy can trigger barge-in. The audit showed four replies followed
by `BARGE_IN` within 6–9 ms; only one of six customer turns in the inspected
session came through the microphone, and it was an 8-character garbled fragment.

Echo/self-interruption is a plausible explanation, not a proven acoustic diagnosis.
Inspect frame handling, playback timing, speaking-state cleanup and barge-in
debouncing together. Do not merely raise the RMS threshold.

### 5.4 Confirmed audit event: duplicate live sockets

Three sockets attached to one call within about 0.3 seconds, producing three
greetings; the third greeting's first audio arrived after about 3.2 seconds.
Check server and client connection ownership. Enforce one active voice socket per
call session and make reconnects replace or reject duplicates deliberately.

### 5.5 Confirmed: no automatic call placement

Call rows are created through `POST /api/orders/{id}/call`. Eligibility,
cooldown, opt-out and trigger-reason logic exist in `backend/app/calls.py`, but no
component scans the order set and initiates calls.

### 5.6 Unresolved: streaming STT failure

A `Streaming STT send failed` event occurred. A 60-second idle-timeout explanation
was considered but **not established**, so do not present it as the cause.
Reproduce or instrument it before fixing.

---

## 6. Live investigation results and limits

These are historical measurements from the earlier investigation, not a fresh
benchmark at your HEAD:

- Two overlapping requests on one Gnani key produced a second-request 429 in all
  three rounds. Six sequential requests all succeeded in roughly 260–400 ms.
- An open STT stream did not block TTS on the same key. Abandoning a TTS stream
  did not leave the next request rate-limited.
- Greeting first audio was 338 ms median alone versus 780 ms median when queued
  behind acknowledgement synthesis. The premature unused 16 kHz acknowledgement
  was subsequently removed; verify that current code no longer repeats it.
- In the inspected owner session, planner latency was roughly 2.3–5.7 s, STT about
  0.4 s, TTS first audio about 0.6–1.0 s, and total server turn time 3.4–7.2 s.
  NVIDIA planner latency is not fixed by adding Gnani keys.
- Whether multiple keys have independent limits remains untested. Do not assume
  that more keys increase account concurrency.
- Timbre produced raw 8 kHz mu-law audio accepted by the telephony probe. Prisma
  transcribed that clean synthetic audio in English, Hindi and Hinglish;
  transcripts arrived about 1.0–1.4 s after speech ended.
- Prisma tagged both Hindi and Hinglish as `hi-IN`; language mirroring therefore
  needs transcript/script analysis rather than blind trust in that tag. Codemix
  formatting preserved English words in Latin script in the probe.

Not verified: Twilio trial support for the required bidirectional stream, actual
call delivery to the owner's Indian handset, real-phone accuracy/noise behavior,
or permission to publish a real-phone demo. Clean synthetic loopback audio is not
evidence of real handset performance.

---

## 7. First deliverable: written analysis before implementation

Produce a concise report covering:

1. The actual order → call → voice turn → confirmation → commit flow, naming real
   functions and files.
2. Evidence for each owner failure and whether sections 5–6 are correct.
3. The smallest reliable turn-taking design: receive-loop ownership, one socket
   per session, stale-frame handling, VAD, playback state and barge-in.
4. The smallest dialer design preserving eligibility, window gating, one-hour
   cooldown, three-attempt cap, opt-out, idempotency and audit behavior. Address
   races between HTTP call creation and the background sweep.
5. How to reuse the voice engine for browser PCM and Twilio 8 kHz mu-law without
   duplicating planner/policy/state-machine logic.
6. A ranked implementation plan with risks and the exact tests/live checks for
   each step.
7. Misleading, unused or README-contradicting code and configuration.

Ask only questions that genuinely block the next step. Do not re-ask decisions in
section 3. Telephony-only unknowns must not delay backend voice repairs.

---

## 8. Implementation order after analysis

Backend correctness first. Keep changes scoped and test each stage before moving
on.

### A. Repair realtime turn-taking and microphone reliability

- Keep the socket receive loop responsive while planning/TTS runs.
- Drop or explicitly handle stale frames; do not replay queued noise as a new
  customer utterance.
- Enforce one active socket per call session and clean reconnect behavior.
- Add measured noise-floor adaptation, sensible bounds and visible audio levels.
- Debounce barge-in with sustained speech and correct speaking-state cleanup.
- Preserve interruption of active agent audio without self-trigger loops.
- Exercise quiet speech, noisy input, long silence, repeated barge-in, reconnect
  and slow-planner cases. Report real measured latency, not estimates.

### B. Make replies contextual and mirror language

- Stop discarding safe model wording on `PROPOSE_PLAN`.
- Give greetings, questions, unclear input and non-plan conversation appropriate
  actions; do not force every utterance into a delivery proposal.
- Validate wording against the policy verdict and plan. Fall back to deterministic
  wording when empty, over-long, OTP-bearing or contradictory.
- Record whether spoken text came from the model or fallback template.
- Mirror English, Hindi and Hinglish, including suitable TTS language settings.
- Two different customer utterances must produce observably different, correct
  replies. Explicit confirmation and all policy guards must remain intact.

### C. Implement the automatic dialer

- Reuse existing eligibility and trigger-reason logic; do not duplicate policy.
- Window gating ON; support the approximately 15-minute demo window and 60-minute
  call-ahead rule without hardcoding a demo date.
- One-hour retry gap, maximum three attempts, no automatic retry of resolved or
  human-owned outcomes.
- Idempotent under repeated sweeps and HTTP/dialer races; one active call per order.
- Respect opt-out, existing cooldown, terminal states and provider concurrency.
- Audit every start and skip reason. Single-process limits must be documented if
  the correctness mechanism is only in-process.
- Prove automatic placement with zero operator clicks using synthetic orders.

### D. Add only the demo-required shop and operator dashboard

- SwiftCart order creation with synthetic customer data and COD/prepaid selection.
- A deliberate demo action to move an order to out-for-delivery.
- Dashboard views for eligible/active/completed calls, live transcript and
  language, dialer reason/skips, model-vs-template wording source, planner action,
  policy verdict/alternatives, confirmation state, courier card and audit trail.
- Build backend contracts first. Visual polish comes after the flow works.

### E. Add Twilio behind explicit verification gates

- First ask the owner whether the basic Twilio console call reached their handset
  and whether Gnani approved the own-phone demo. Never ask for credentials in chat.
- Request approval immediately before each live call test.
- Use a public webhook/stream URL from local secret configuration; do not print
  secret URLs or credentials in committed artifacts.
- Bridge Twilio Media Streams to the existing voice engine using 8 kHz mu-law,
  including playback tracking and clear/barge-in behavior.
- Keep the browser path operational as a fallback.
- Treat trial streaming, trial announcements, foreign caller ID/spam filtering and
  Indian call delivery as unverified until exercised.

For every stage: add tests that fail without the change, keep the existing suite
green, update README behavior claims, and report files changed, commands run,
measured results and remaining limits. Do not start a broad frontend redesign in
this task.
