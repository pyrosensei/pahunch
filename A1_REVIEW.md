# A1 review — 31 October 2026

A1 is implemented and exercised locally. Work stops here for owner review.
A2, conversational wording/language changes, the dialer, shop/dashboard APIs and
Twilio integration have not started. No dependencies were installed, no external
provider requests or live calls were made, and nothing was committed or pushed.

## Changes

- The socket reader runs independently of utterance processing and TTS. It
  snapshots each utterance, permits one active turn, rejects overlapping scripted
  turns, and consumes/counts frames received during planning rather than replaying
  them as a later utterance.
- One socket owns a session per application process. Duplicates receive an
  audited error and close 4409 before greeting/provider setup; ownership is
  released after cleanup so the existing call can reconnect.
- A separate STT sender holds at most 50 queued frames or one second of queued
  PCM, excluding one in-flight send. Overflow cancels that sender and falls back
  to REST using the captured utterance. Connect/send/finish/cancel/close phases
  are bounded and failures are audited by phase and exception type. The provider
  transcript queue is bounded to 16 segments; overflow raises an explicit error.
- Output messages carry generation IDs. Reset and barge-in invalidate a
  generation before cancellation, preventing late audio from restarting it.
  Cancellation during acknowledgement wait also cancels/awaits the child planner.
  Repeated resets retain ownership of tasks still cleaning up. Disconnect awaits
  output, planner, greeting, acknowledgement and STT cleanup. A provider socket
  acquired before a cancelled handshake is closed.
- The browser opts into playback tracking. Synthesis completion and final playback
  drain are separate; underruns cannot send a premature start. Worklet connection
  IDs prevent old drain callbacks from affecting a replacement socket. The client
  ignores obsolete socket/audio/decode callbacks and releases microphone resources,
  including tracks whose permission request finishes after disconnect.
- Timing starts at the utterance endpoint for streaming and REST fallback paths,
  so first_audio_ms and total_ms include streaming finalization/fallback waits.
  Acknowledgement preparation follows greeting synthesis rather than competing
  with it. VAD thresholds, endpointing and the existing barge-in energy test are
  unchanged.

## Files changed

| Files | Purpose |
|---|---|
| `backend/app/main.py` | Socket ownership and duplicate rejection |
| `backend/app/realtime.py` | Responsive reader, owned turn tasks, bounded STT sender, generations, playback acknowledgement, audits and cleanup |
| `backend/app/voice.py` | Child-planner cancellation and endpoint-based timing for REST fallback |
| `backend/app/streaming_stt.py` | Bounded transcript queue, cancelled-handshake cleanup and observable close failures |
| `backend/static/index.html`, `backend/static/pcm-player.js` | Playback generations, final-drain handshake, reconnect guards and resource cleanup; layout unchanged |
| `backend/tests/test_realtime_a1.py` | 16 new A1 regression cases, including timing and repeated-reset races |
| `backend/tests/test_streaming_stt.py` | Two new regressions for handshake cancellation and transcript overflow |
| `backend/tests/test_realtime.py` | Existing barge-in test now verifies the additive generation ID; confirmation/interruption assertions retained |
| `backend/tests/voice_browser.test.cjs` | Nine browser/worklet regressions using Node's built-in test runner and browser API fakes |
| `README.md`, `VOICE_REPAIR_ANALYSIS.md`, `A1_REVIEW.md` | Current protocol/limits/test commands, approved amendments and this report |

The supplied untracked `AGENTS.md` and `CODEX_PROMPT.md` were left unchanged.
No database, recording, generated evaluation report, credential or configuration
file was changed as part of A1. The analysis and this review remain uncommitted.

## Commands and results

Commands below ran from `backend`, except `git diff --check`, which also ran from
the repository root. Tests use the existing virtual environment.

| Command | Actual result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider` | Baseline **149 passed, 1 warning in 40.22s**; final **167 passed, 1 warning in 52.72s** |
| `.\.venv\Scripts\python.exe -m pytest tests/test_realtime_a1.py -q -o addopts= -p no:cacheprovider --tb=short` | Initial nine cases: **9 failed** against the old backend; subsequent targeted runs passed as cases were added. All 16 final cases are included in the full suite above. |
| `.\.venv\Scripts\python.exe -m pytest tests/test_streaming_stt.py -k 'handshake_closes or flood' -q -o addopts= -p no:cacheprovider --tb=short` | **2 failed** before the STT repairs; both pass in the final full suite |
| `.\.venv\Scripts\python.exe -m pytest tests/test_realtime_a1.py -k timings -q -o addopts= -p no:cacheprovider --tb=short` | **2 failed**, exposing omitted stream-finish wait; both pass after the timer repair |
| `node --test --test-reporter=tap tests/voice_browser.test.cjs` | Original six new regressions failed before client/worklet repairs; the added reconnect case also reproduced a failure. Final: **9 passed, 0 failed** |
| `.\.venv\Scripts\python.exe -m pytest tests/test_realtime_a1.py::test_reader_handles_controls_and_drops_frames_during_planning -q -s -o addopts= -p no:cacheprovider` | **1 passed**; printed measurements below |
| `$env:PYTHONPATH = (Get-Location).Path; .\.venv\Scripts\python.exe ../evals/runner.py` | **12/12 scenarios passed** using an in-memory database and scripted proposals |
| `.\.venv\Scripts\ruff.exe check app/realtime.py app/main.py app/voice.py app/streaming_stt.py tests/test_realtime_a1.py tests/test_realtime.py tests/test_streaming_stt.py --no-cache` | **All checks passed** |
| `git diff --check` | Passed; Git emitted existing LF/CRLF conversion warnings |

Intermediate integration checks caught four failures (one additive-protocol
expectation, two stale ORM observations in the new harness, and a new fake's
attribute-name collision). These were corrected openly without weakening the
existing behavior assertions. Lint initially found ten issues, including an
unbound closure variable; all were fixed. One timing edit briefly duplicated a
keyword argument and produced three collection errors in the targeted command
`.\.venv\Scripts\python.exe -m pytest tests/test_realtime_a1.py tests/test_realtime.py tests/test_voice_api.py -q -o addopts= -p no:cacheprovider --tb=short`;
after correction that command passed **51 tests**, followed by the final full run.

The remaining Python warning is the existing Starlette TestClient/httpx
deprecation. Dependency changes were not attempted.

## Measurements and limits

The final diagnostic run printed **0.05 ms** for a control response while the
planner was held on an asyncio event, and **first_audio_ms = 11** after releasing
that event. These are single observations with in-memory transport and scripted
speech/model components. They prove responsive control handling before planner
completion; they are not handset, real microphone, provider latency or percentile
measurements. The timing regressions separately verify that measured stream-finish
wait is included in both streaming and REST-fallback turn timing.

The browser tests execute the actual worklet/page JavaScript with mocked browser
APIs. STT framing tests use local loopback WebSocket servers. No real browser
audio device, acoustic echo, noisy/quiet microphone, Prisma/Timbre/NIM service or
telephone path was exercised. No live matrix or old published benchmark was
regenerated.

Known remaining limits:

- A2 remains necessary: fixed VAD, single-frame energy barge-in, no pre-roll,
  existing utterance-cap/minimum-burst behavior and absent no-speech feedback.
  A1 prevents stale generations from resuming; it does not establish acoustic
  echo rejection or quiet/noisy microphone reliability.
- Socket ownership is local to one application process. Multi-worker operation
  does not share that registry.
- Disconnect cleans transport/resources and preserves the logical call row for
  deliberate reconnect. Abandoned-call expiry and audited retry dispositions
  belong to later dialer work and are not implemented.
- Frames spoken during a busy turn before playback are deliberately dropped and
  audited. Supporting conversational overlap during planning would be a separate
  behavior decision.
- Legacy clients without playback_tracking rely on explicit start handoff.
  Updated clients use generation-specific final playback acknowledgements.
- Proposal wording, confirmation policy, language mirroring and commit ownership
  retain their previous behavior. The browser still performs the explicit HTTP
  commit after confirmation. No automatic call placement or phone bridge exists.

The approved future designs remain recorded: server-controlled plan facts with
validated placeholders, natural RESPOND messages, wording_source/fallback_reason,
and dialer auditing only for starts/disposition/skip-reason changes with separate
current skip state. None was implemented in A1.
