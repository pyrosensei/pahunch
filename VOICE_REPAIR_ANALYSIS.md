# Voice backend repair analysis

Reviewed on 31 October 2026, before implementation. This is a design and code
review, not a claim that the repairs or telephone demo work.

Approved amendments: implement A1 (socket/task ownership, bounded STT, cancellation,
playback generations and disconnect cleanup) first and stop for review. Adaptive
VAD, pre-roll, utterance-cap correction and no-speech/onset/endpoint work are A2.
Proposal wording will use model-written wrappers around server-filled validated
placeholders, not a multilingual natural-language parser. Dialer audits will
record starts, disposition changes and skip-reason changes; current skip state
will be exposed separately. The initial implementation pass covered A1 only.
After the A1 report, the owner authorized A2 as the next step. See `A2_REVIEW.md`
for its detector changes, synthetic measurements and limits.
After the A2 report, the owner authorized Stage B as the next step. Its contextual
wording, language and safety changes are recorded in `B_REVIEW.md`. After that
review, the owner authorized Stage C. Its local scheduler, shared admission,
recovery and audit changes are recorded in `C_REVIEW.md`. The owner subsequently
authorized D and the locally tested E adapter. See `D_REVIEW.md` and `E_REVIEW.md`.
Live phone verification remains gated: the owner reported Gnani permission is
pending, and console/account verification is unconfirmed. A permission email was
drafted, not sent. The original findings below describe the pre-repair baseline.

## 1. Current flow and baseline

`main.create_app()` starts the database and provider clients, but no order sweep.
The browser's `startCall()` posts to `/api/orders/{order_id}/call`.
`CallService.create_call()` runs `_check_eligibility()` and `_trigger_reason()`,
creates a session, audits it and transitions CREATED to CALLING. The browser then
calls `connect()` and `confirm_context()` through HTTP to reach DISCOVERY.

`main.voice_socket()` accepts a socket and calls `realtime.handle_voice_socket()`.
The handler builds `VoiceTurnService`; `config` schedules `greet()` and
`prepare_ack()`. `start` enables capture, potentially after the greeting's
synthesis completes. Incoming PCM goes through `UtteranceBuffer.add()` and
`streaming_stt.GnaniStreamingSTTClient.send_audio()`. At endpoint,
`process_utterance()` finishes streaming STT or uses REST transcription, then
`VoiceTurnService.handle_transcript()` invokes `PlannerService.handle_turn()`.

The planner applies safety, explicit-confirmation, confidence, action-permission
and plan-normalisation checks. `_execute_decision()` sends proposals to
`CallService.submit_turn()`, which calls `policy.validate_plan()` and persists the
proposal and verdict. A valid proposal reaches PENDING_CONFIRMATION. A later
explicit agreement sets `customer_confirmed`; it does **not** commit on the
server voice path. The browser's `turn` handler then calls its `commitPlan()`.
`CallService.commit_plan()` checks confirmation, revalidates current policy,
writes the plan/card, transitions to COMMITTED and audits the result. The phone
adapter must eventually perform this same commit orchestration without a browser.

The working branch is `feat/voice-latency-and-demo`, with the 11 commits described
in the task. Before this report, the only untracked files were the supplied
`AGENTS.md` and `CODEX_PROMPT.md`; tracked files were unchanged.

Commands executed from `backend`:

- `python -m pytest -q`: failed collection, eight errors because system Python
  lacks `sqlmodel`; it also reported pytest cache permission warnings.
- `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`: passed the suite;
  one Starlette TestClient deprecation warning.
- `.\.venv\Scripts\python.exe -m pytest --collect-only -q -o addopts= -p no:cacheprovider`:
  **149 tests collected**. This establishes the passing suite count above.
- `$env:PYTHONPATH = (Get-Location).Path; .\.venv\Scripts\python.exe ../evals/runner.py`:
  **12/12 deterministic scenarios passed**, using in-memory SQLite, scripted
  proposals and explicit confirmation flags. These are not STT or LLM tests.

No dependencies were installed. No live provider requests, phone calls, tunnel
changes or live matrix runs were made. No new latency measurement is available.

## 2. Evidence for the failures and corrections to the handoff

| Finding | Evidence in this tree and limits |
|---|---|
| Repeated template | Confirmed: `_execute_decision()` ignores `decision.message` for every PROPOSE_PLAN and calls `plan_result_message()`. `test_valid_proposal_uses_backend_message_and_waits_for_confirmation` explicitly pins this old behavior. |
| Wrong conversational action | There are four `PlannerAction` values and no general response action. `_PLANNER_RULES` describes clarification for ambiguous/missing delivery details and forces Hinglish. `_recent_turns()` already supplies conversation history. Historical greeting/question misclassification is reported evidence, not a new live reproduction. |
| Fixed VAD | `UtteranceBuffer.add()` compares RMS with a fixed threshold; default is 500. There is no calibration. Quiet speech can stay undetected and constant noise can remain classified as speech. The reported 10–25 seconds is not independently attributed to these mechanisms. |
| Blocked receive loop | Confirmed: binary input, `stop`, and `scripted_turn` await complete STT/planner/TTS processing. The same loop cannot consume controls or audio during that wait. |
| Stale frames and interruption | Confirmed mechanism: `speaking` becomes true on chunks/blob delivery and is not cleared by `send_audio_end()`. A single subsequent energetic frame enters barge-in. Normal reply generation is not cancelled by barge-in because the receive loop cannot run until it completes. Actual acoustic echo remains an inference. |
| Duplicate sockets | No server ownership registry; browser `connectSocket()` overwrites `state.ws` without closing its predecessor, and order clicks are not serialized. This permits duplicates. The historical three-socket timings were not remeasured. |
| No automatic placement | Confirmed: application startup has no sweep or telephony dispatch. Creating a CALLING row currently requires HTTP/operator action. |
| Missed-detection feedback | Correction: the worklet already emits `level`, and `ensureMic()` already displays a local VU meter. What is missing is server detection/noise-floor feedback, a no-speech hint and a distinction between capture, recognition and planner failure. |
| Streaming STT error | `send_audio()` wraps all send failures as `Streaming STT send failed`; useful close-phase/type diagnostics are absent. REST fallback is present. There is no evidence here establishing a 60-second idle timeout as the cause. |
| Premature acknowledgement | The unused pre-config 16 kHz preparation is absent. However, `config` schedules greeting and acknowledgement preparation together, so they can still contend for the same speech slot. |
| Playback ownership | `pcm-player.js` emits `drained` whenever its queue temporarily empties. The browser treats this as completion even if TTS is still generating later chunks. `agent_audio_end` also clears the browser speaking flag before queued playback necessarily finishes. These are separate generation and playback lifetimes. |

Additional correctness gaps relevant to these repairs:

- The utterance cap uses `speech_ms + silence_ms`, but silence resets on speech;
  this is not total buffered elapsed time. Endpoint handling also requires
  `has_utterance`, so short rejected bursts need an explicit reset/buffer bound.
- Closing a browser socket does not finish the call row. An active row can block
  later eligibility indefinitely. Reconnect grace must not silently create a new
  attempt or mark a committed call unreachable.
- `ASK_CLARIFICATION` does not increment `clarification_count`; invalid policy
  proposals do. Total-turn enforcement already exists. Its terminal result can
  still be followed by normal proposal wording unless response selection checks
  the resulting outcome first.
- `safe_model_message()` is a length/keyword filter, not semantic plan grounding.
  It cannot by itself prove that a different recipient, time or permission was
  not promised. Customer text is stored verbatim by `submit_turn()`; existing
  refusal matching alone does not guarantee redaction of volunteered OTP digits.

Section 6's provider timings and concurrency/audio-format results remain
historical observations. The probe implementation supports the described
synthetic experiments, but running unit tests does not reproduce those results
or establish handset accuracy or independent account limits across keys.

## 3. Smallest reliable turn-taking design

Keep one browser socket reader, one bounded STT sender and one active turn task.
The reader must never await planner completion or complete audio generation.
Snapshot an utterance before starting its task; frames received during planning
are discarded and counted rather than buffered for the next customer turn. Bound
the STT queue; if the provider cannot keep up, record the overflow and use the
captured utterance for REST fallback. Do not concurrently mutate one SQLModel
Session from separate workers; use short, separately owned database operations.

Use an application-owned socket registry: deliberately reject a second socket
for an occupied session, with an error and close code 4409, before greeting or
opening another provider stream. Release ownership only after tasks and streams
have finished cleanup. The browser serializes connection attempts and ignores
callbacks from obsolete sockets. Reconnect uses the existing call row.

Track capture, planning, audio generation and playback separately. Give every
output a generation ID. `agent_audio_end` marks generation complete; a browser
playback acknowledgement marks actual completion only after that generation's
last sample drains. Queue underruns must not reset capture. Flush/cancellation
invalidates old generations so late chunks cannot restart interrupted speech.
Keep bounded pre-roll for sustained barge-in, cancel active output, and start a
fresh utterance once; isolated energy spikes must not interrupt. Preserve real
speech during the greeting using this same mechanism. Await task cancellation,
including `VoiceTurnService`'s child planner task, and record cleanup failures.

Measure baseline synthetic PCM sequences before replacing VAD. Estimate a bounded
noise floor from non-speech listening frames, use a relative threshold with
absolute bounds, and freeze adaptation during speech and agent playback. Detect
on sustained energy, keep pre-roll, and cap by actual buffered duration. Reset
sub-minimum bursts rather than accumulating them. Emit throttled server RMS,
threshold, calibration and no-speech events for visible feedback and audit
summaries. Constant-noise and quiet-speech tests establish limits; an energy
detector cannot guarantee separation of speech from all nonstationary noise.

## 4. Smallest correct dialer

Start one in-process sweep in `create_app()` lifespan, with explicit shutdown.
Use `CallService` for eligibility and trigger classification. Automatic candidates
must be OUT_FOR_DELIVERY and have a risk trigger; generic DELIVERY_CONFIRMATION
does not qualify. Recommended window predicate is
`window_start - 60 minutes <= now < window_end`: this includes an ongoing,
unexpired window and avoids missing an order that becomes ready after its start.
Use timezone-aware comparisons consistent with policy's interpretation of naive
order timestamps. The shop creates windows relative to current time, starting
15 minutes later, without moving the historical evaluation fixtures.

Count call attempts from CallSession rows, including manual attempts;
`Order.prior_attempts` means failed **delivery** attempts and must not be reused
as the telephone-attempt counter. Cap at three. Enforce the one-hour gap
conservatively from the latest attempt's end (or start if no end exists), also
respecting `last_call_completed_at`. Existing cooldown is only 30 minutes.
Block committed, escalated and turn-budget-exhausted history; retry NO_ANSWER
and UNREACHABLE only after eligibility/cooldown. Retry TOOL_FAILURE only with an
explicit recorded transient disposition; an unknown failure is not retryable.

Put the check-and-create critical section in `CallService.create_call()` so
HTTP and the sweep use the same process-wide order lock and fresh database
reads. A sweep-only lock would not prevent the HTTP race. With no extra worker,
document one application process and one active demo call as correctness limits;
key count is not evidence of independent provider capacity. Reserve capacity
before dispatch; repeat sweeps see the existing active call. Recover abandoned
CALLING/connected sessions through explicit, audited timeout/disconnect
dispositions, never by silently deleting them.

`AuditEvent.session_id` is currently a required session foreign key. It cannot
represent a skipped order that has no call. Add a small order-scoped dialer-event
table with optional session linkage for CANDIDATE, SKIPPED and STARTED, plus
reason/time. Do not create fake call sessions to audit skips. Audit decisions
when a call starts, its disposition changes, or the skip reason changes. Expose
current skip state separately; unchanged sweeps must not duplicate audit events.

Creating a browser fallback session proves automatic scheduling; it does not
prove that a telephone rang. A call is dispatched only after the selected
transport accepts it. Browser attachment, provider acceptance, answer, context
confirmation and customer plan confirmation remain distinct audited events.

## 5. Reusing the voice engine for Twilio

Extract the repaired session coordination from browser message encoding only
where the second transport needs it. Both adapters call `VoiceTurnService`,
`PlannerService` and `CallService`; neither implements policy or confirmation.
For phone input, decode 8 kHz mu-law to PCM16 at 8 kHz and configure the existing
STT/VAD path for that rate. Request native mu-law Timbre output where supported,
including a tested blocking fallback, so no unnecessary resampling is needed.
The probe's mu-law decoder is existing reusable evidence; it is not a bridge.
Do not rely on the removed Python `audioop` module or install a codec dependency
without approval.

Twilio's bidirectional stream uses `<Connect><Stream>`. Its outbound audio is
base64 raw mu-law/8000 with no file header; `mark` tracks playback and `clear`
flushes it for interruption. Map these to the same generation/playback state
used by the browser. Validate signed webhooks and the WebSocket handshake,
bind stream and provider call identifiers to the expected local session, and
deduplicate/reject mismatched callbacks. Source:
[Twilio Media Streams messages](https://www.twilio.com/docs/voice/media-streams/websocket-messages),
[Twilio webhook security](https://www.twilio.com/docs/usage/webhooks/webhooks-security).

Use existing `httpx` for call creation after approval, with credentials and the
allowlisted handset read only through Settings. Never expose them in response
models, exception bodies, logs, report artifacts or browser markup. An ambiguous
provider-create timeout must not be blindly retried: reconcile provider state or
require operator review to avoid duplicate ringing. Verify console-call delivery
and Gnani's permission before a real-phone demo; request approval immediately
before each live call test. Trial capability, carrier delivery and permission
remain unverified.

## 6. Ranked implementation and checks

| Stage | Change and principal risk | Tests/checks before proceeding |
|---|---|---|
| A | Responsive reader, bounded STT, socket ownership, playback generations, adaptive VAD; risk is dropped speech or cancellation races. | Add regression tests in `tests/test_realtime.py` and `tests/test_streaming_stt.py` for slow planner/TTS with concurrent controls, discarded planning frames, bounded queues, duplicate/reconnect, greeting interruption, short spikes, repeated sustained barge-in, late chunks, disconnect cancellation, STT phase errors, quiet speech, constant noise, short bursts and long silence. Add a Node-based worklet harness for underrun versus final drain. Measure synthetic onset/endpoint times and wall-clock first_audio_ms separately; live microphone/provider checks require approval. |
| B | Contextual actions, safe model wording, language mirroring and wording-source audit; risk is an unvalidated promise. | Add a RESPOND action for greetings/questions without a proposal. Keep existing explicit-confirmation acceptance unchanged. Extend `tests/test_planner.py`, `tests/test_voice_api.py` and `tests/test_gnani.py` with two distinct contextual replies, questions/greetings, rejected-plan correction, terminal wording, English/Devanagari/Roman Hinglish, all fallback causes, volunteered OTP redaction, and language-sensitive TTS/cache keys. Assert unknown or contradictory proposal wording falls back and no invalid plan confirms or commits. |
| C | Shared admission, eligibility sweep, retries, dispositions and order audit; risk is duplicate attempts or recalling a resolved order. | New `tests/test_dialer.py`: injected clock, exact 60-minute/window-end/one-hour boundaries, status/risk/opt-out/missing metadata, three attempts, all outcomes, transient versus unknown failure, restart, capacity, repeated sweeps, and simultaneous HTTP/sweep creation using separate sessions against a temporary file database. Lifespan test proves a synthetic eligible order produces a browser fallback session with zero operator requests. |
| D | Minimal synthetic shop and dashboard contracts; risk is showing stale state or treating session creation as phone delivery. | HTTP tests create COD/prepaid orders, inspect relative windows, mark ready, poll active/completed calls, transcripts, source/action/verdict, confirmation/card and dialer decisions. Browser exercise checks the complete synthetic flow. Render untrusted transcript/model text with textContent rather than current log() innerHTML. Use local files and existing static hosting; no broad redesign or Sites project. |
| E | Twilio adapter behind owner verification; risk is duplicate dialing or external data exposure. | Mock HTTP and signed webhook fixtures; codec reference vectors, frame/sample-rate checks, stream binding, playback mark/clear, callback replay/reordering, failed dispatch, ambiguous timeout and masked outputs. Then approved synthetic provider loopback/matrix, followed by an individually approved owner-handset test. Never label mocked or synthetic audio as real phone performance. |

For B, keep plan facts server-controlled. Model-written contextual wording may
wrap validated placeholders; the backend inserts the validated window, recipient
and action summary. Reject unrecognized or contradictory wrappers and use the
language-matched deterministic fallback. RESPOND may use natural model responses
for greetings/questions. Record wording_source and fallback_reason. Do not build
a multilingual natural-language parser or use a second LLM as a policy judge.

At each stage run the full existing suite plus new regressions, then the
in-memory deterministic scenarios. Proposed stage command from `backend`:
`.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider`.
Run new regressions against the unchanged behavior first to demonstrate failure;
never silently weaken the old tests. Update README after each behavior change.
Use a newly created disposable database for live matrix checks because
`matrix_runner.py` deletes its target's rows. Existing database files and reports
must not be overwritten as a shortcut. Live provider use is pending approval
under AGENTS.md section 3.

Proposed public contract additions, for review under AGENTS.md section 11:

- Browser voice: generation IDs on output; a `playback_done` client control;
  server `audio_level` and `no_speech` events; duplicate connection close 4409.
  Existing message names and the PCM16 input format remain supported.
- Planner/voice metadata: RESPOND action, response language, wording_source and
  fallback_reason. These are observable schema additions.
- `POST /api/orders` accepts only demo choices such as payment mode, not personal
  details, and returns the synthetic Order; `POST /api/orders/{id}/out-for-delivery`
  performs the deliberate readiness action.
- `GET /api/sessions` returns bounded/filterable operator call summaries;
  `GET /api/sessions/{id}/turns` returns ordered transcript/decision metadata;
  `GET /api/dialer/events` returns bounded order-scoped start/skip history.
- Stage E adds signed Twilio answer/status webhook routes and a bound Media
  Streams socket; these remain disabled until verification gates are met.

## 7. Misleading, unused and contradictory surfaces

- README still says 142 tests; the current collected suite has 149. Its live
  matrix numbers are historical and cannot be presented as this branch's result.
- README describes browser calling and telephony as optional; the new target is
  an operator dashboard plus owner-handset transport, with browser fallback.
  The Twilio settings are currently unused placeholders, not working telephony.
- README's protocol table omits config, streaming chunks/end, scripted input and
  acknowledgement details already present at HEAD.
- README's broad noise-robustness claim exceeds the fixed-threshold VAD's tested
  guarantees. `pcm-player.js`, `gnani.py` and `http_pool.py` also contain historical
  latency claims in comments; they are not fresh benchmarks.
- Configuration/comments assert that each additional key adds a concurrency
  slot. The implementation sizes a semaphore by key count and rotates keys,
  but that neither establishes independent account limits nor reserves each key
  until its specific request completes. One key is sufficient for this demo.
- TTS language, greeting, acknowledgement and most policy/confirmation messages
  are fixed to Hinglish. Per-turn language must reach all synthesis paths and
  cache keys, not only the planner prompt.
- `_user_prompt()` calls the order window's date `call_date`, so "today" can mean
  a future delivery date. Historical fixtures use fixed November windows; they
  are not a dynamic shop or current-time dialer demonstration.
- `safe_model_message()` checks neither plan consistency nor all scripts.
  Existing OTP question refusal must not be mistaken for complete prevention of
  OTP persistence or output.
- Closing the UI's End call button only closes the socket. Session state remains
  active. Context confirmation is currently an operator HTTP transition, not a
  proven consent conversation for a live telephone call.
- `websockets` is directly imported but not directly declared in pyproject.toml;
  it is currently supplied through `uvicorn[standard]`. No dependency change is
  needed for the initial repairs.

The owner approved the additive browser protocol, RESPOND metadata, demo-order
and read-only dashboard contracts, with the amendments above. The implementation
pass initially stopped after A1; the subsequent next-step authorization covers A2.
Twilio integration,
live calls, dependency installation and a broad UI redesign are not authorized.
