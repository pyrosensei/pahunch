# Synthetic provider test review

31 October 2026. This pass tests the repaired application without a handset.
Gnani Timbre, Prisma streaming/REST STT and NVIDIA NIM are real. Customer speech
is synthesized, orders are fictional SwiftCart data, and SQLite is isolated in
new scratch directories. Twilio HTTP, media delivery and playback completion
are simulated locally. No real phone call, account change, tunnel, deployment,
email, dependency installation or change to the actual `.env` was performed.

## Faults found and corrected

1. The planner could acknowledge a delivery instruction as RESPOND instead of
   returning a structured proposal. Its output-shape prompt also required fields
   omitted from the provider schema. The prompt now matches the schema, explains
   action classification and includes examples grounded in the current order.
   Policy still determines legality, including rejecting the security-drop example
   for COD orders. No natural-language plan parser was added.
2. Prisma's verbatim output rendered spoken English confirmation as Devanagari
   phonetics. The strict existing confirmation gate rejected that transcript.
   A controlled comparison using the same synthesized audio showed `codemix`
   preserved English and Hindi in their respective scripts, so the streaming
   handshake now explicitly requests that format. Confirmation vocabulary,
   thresholds and policy checks were not broadened.
3. A factual question initially produced a model-written delivery time. The
   wording guard correctly rejected it, but the resulting clarification was
   unhelpful. A factual RESPOND example and the output instructions now retain
   the literal `{order_window}` placeholder for backend substitution. Raw model
   facts remain rejected. Unsafe greeting/audio-check wording now falls back to
   the matching localized greeting/audio-check response.

The controlled transcription comparison gave:

| Synthesized speech | Verbatim transcript | Codemix transcript |
|---|---|---|
| Yes, please go ahead. | यस, प्लीज गो अहेड। | Yes please go ahead |
| Hello, can you hear me? | हैलो, कैन यू हियर मी? | Hello, can you hear me? |
| हाँ ठीक है, आगे बढ़ाइए। | हाँ, ठीक है, आगे बढ़ाइए। | हाँ, ठीक है। आगे बढ़ाइए। |

This is three controlled samples, not a multilingual accuracy benchmark.

## Verification commands and results

Commands below ran from `backend` using the existing virtual environment.
Network scripts and sanitized per-turn results remain locally under ignored
`.artifacts/`; they are scratch evaluation tools rather than published fixtures.

| Command | Observed result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider` before edits | 352 passed, 1 warning, 76.76 s |
| Same full-suite command after final code edits | 360 passed, 1 warning, 73.04 s |
| `.\.venv\Scripts\python.exe -m pytest tests/test_conversation.py tests/test_planner.py tests/test_streaming_stt.py -q -o addopts= -p no:cacheprovider` | 123 passed, 13.64 s |
| `node --test --test-reporter=tap tests/voice_browser.test.cjs tests/demo_browser.test.cjs` | 23 passed, 0 failed |
| `.\.venv\Scripts\python.exe ..\evals\runner.py` | 12/12 deterministic scenarios passed; scripted plans, no model or speech |
| `.\.venv\Scripts\python.exe -m scripts.vad_probe` | Generated PCM: 100 ms onset, 600 ms endpoint for three speech-level cases; zero false onsets for tested steady noise; intermittent cap retained exactly 1000 ms including pre-roll |
| `.\.venv\Scripts\ruff.exe check app/planner.py app/streaming_stt.py tests/test_conversation.py tests/test_streaming_stt.py --no-cache` | All checks passed |
| `git diff --check` | Passed; Git emitted line-ending normalization notices |
| `.\.venv\Scripts\python.exe .artifacts/provider_probe.py` | 5/5 real-provider probes passed |
| `.\.venv\Scripts\python.exe .artifacts/format_probe.py` | Three utterances compared in both formats, no reported errors |
| `.\.venv\Scripts\python.exe .artifacts/synthetic_eval.py factual_question,changed_confirmation` | 2/2 after the factual-answer prompt correction |
| `.\.venv\Scripts\python.exe .artifacts/synthetic_eval.py` with final code | 8/8 scenarios, 14 recognized speech turns; no reported audio failures |

The suite warning is the existing Starlette TestClient/httpx deprecation. No
dependency was installed to silence it. Regression checks were observed failing
before their fixes: five initial contextual/prompt checks, the missing-format
handshake assertion, then four factual-example checks (three new parameter cases
and the expanded example-list expectation). Existing policy/confirmation
assertions remain intact.

Initial sandbox-only network attempts could not reach the providers; authorized
network retries worked. The first end-to-end harness also attempted a prepaid
order without a risk trigger: the scheduler correctly skipped it. The synthetic
fixture was corrected to an OTP-required prepaid order. A subsequent run passed
4/6 and exposed the English transcription problem; the next passed 6/6. The
extra factual check then failed safely before the prompt correction above.
Failures are retained locally rather than replacing the historical matrix report.

## Final combined run

All eight scenarios passed together after the final prompt change. The four
expected delivery commits produced courier cards; no other scenario committed.
Both unsafe COD security requests received `COD_REQUIRES_DIRECT_HANDOFF`.

| Scenario | Observed outcome | First audio per turn, ms |
|---|---|---|
| English direct handoff then explicit yes | COMMITTED | 3027, 723 |
| Hindi direct handoff then explicit yes | COMMITTED | 3112, 688 |
| Hinglish unsafe security drop, direct correction, yes | Refusal, fresh valid offer, COMMITTED | 3026, 1009 (ack), 851 |
| Greeting / audio check | Natural RESPOND, no commit | 3030 |
| OTP question | Deterministic refusal, no commit | 704 |
| Native 8 kHz phone-format loopback | COMMITTED; Twilio and playback mocked | 2493, 700 |
| Scheduled-window question | Server-filled fact template, no commit | 1692 |
| Offer then yes with a security-drop change | New invalid proposal; unconfirmed, no commit | 2727, 921 (ack) |

Final `first_audio_ms` ranged from **688 to 3112 ms**, with two cached
acknowledgements explicitly marked above. Full turn completion ranged from
1407 to 5831 ms. An earlier six-case run measured first audio up to 5054 ms;
provider variance remains visible. These are observations of individual runs,
not performance guarantees or a measured improvement against the historical
matrix. Endpoint/device/carrier exclusions are explained below.

Sanitized final per-turn record:
`backend/.artifacts/synthetic-20261031T114339Z/results.json`.
Provider and format comparisons are in `.artifacts/provider-probe.json` and
`.artifacts/format-probe.json`, relative to `backend`. These files are ignored.

## Direct provider measurements

These are single requests, not percentile estimates. TTS first audio is measured
from the provider request, independently of the full voice-turn timing.

| Probe | Result |
|---|---|
| Streamed PCM16, 16 kHz | First audio 362 ms; complete 951 ms; 81,920 bytes |
| Streamed native mu-law, 8 kHz | First audio 171 ms; complete 598 ms; 21,120 bytes |
| Blocking native mu-law, 8 kHz | Complete audio ready 658 ms; 22,400 bytes; no WAV header |
| REST STT | English confirmation recognized, existing confirmation guard accepted it; 495 ms |
| Planner greeting | RESPOND, accepted model wording, no fallback; 1,086 ms |

The network tests deliberately used only the primary Gnani key and the configured
NIM key. Final eight-scenario run: customer TTS 13 HTTP 200, server speech 27 HTTP
200, planner 9 HTTP 200; no non-200 HTTP responses recorded, one mocked Twilio
creation and zero real Twilio requests. WebSocket STT also processed all 14 turns.
These sequential tests do not measure concurrent call capacity,
prove account quotas, or show that extra keys increase the provider's allowance.

## Files changed in this pass

| File | Change |
|---|---|
| `.gitignore` | Ignore scratch `.artifacts/` scripts, databases and model results |
| `backend/app/planner.py` | Correct prompt/schema shape, action/fact examples and contextual fallback |
| `backend/app/streaming_stt.py` | Request codemix streaming transcription |
| `backend/tests/test_conversation.py` | Eight additional contextual/prompt parameter cases |
| `backend/tests/test_streaming_stt.py` | Pin the transcription-format handshake |
| `README.md` | Current test count, synthetic-provider scope, key clarification and limits |
| `SYNTHETIC_TEST_REVIEW.md` | This review |

Earlier A1-E changes already present in the working tree were preserved. Nothing
was committed or pushed. Scratch databases/results are ignored and were not
staged. Existing project databases were not reset.

## Limits and remaining work

- Real handset delivery, Twilio account/streaming entitlement, public ingress,
  proxy signatures, consent on a physical call and carrier timing remain untested.
  Fake configuration enabled phone gates only inside the isolated test process;
  real configuration and approval gates were not changed.
- The synthetic harness acknowledges playback programmatically. It proves media
  protocol flow and byte production, not audible quality, acoustic echo handling
  or human barge-in. A speech recognizer received synthesized audio, not typed
  customer turns, but this does not establish human/noisy-speech accuracy.
- UI DOM/HTTP tests passed. Visual inspection remains unavailable because the
  computer-use native/browser host is disconnected.
- Latency remains open. `first_audio_ms` starts at the server's utterance endpoint;
  it excludes VAD endpoint delay, output-device playback and carrier delay. A
  cached acknowledgement can precede the substantive answer. Several measured
  turns take multiple seconds; sub-second conversation performance is not proved.
- Hinglish audio can yield Hindi or mixed-script transcription and therefore a
  Hindi reply. Consistent Roman Hinglish mirroring is not established by this run.
- The historical 21-case live matrix was not rerun. Representative synthetic
  checks and passing regressions do not prove every live-provider path.
