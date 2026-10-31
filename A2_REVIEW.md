# A2 review — 31 October 2026

A2 is implemented: adaptive energy detection, onset pre-roll, corrected utterance
limits, sustained barge-in and visible microphone feedback. Conversation wording,
language mirroring, dialer/shop/dashboard work and Twilio integration have not
started. Existing A1 changes are preserved. No dependency installation, external
provider requests, live calls, commits or pushes were performed.

## Behavior

- A fresh socket learns the room's energy floor from 300 ms of listening audio.
  It uses the weighted lower 20th percentile of up to 50 recent idle frames, a
  3x noise margin and absolute RMS bounds of 120–12000. Learning freezes during
  candidate speech, active utterances and playback. Calibration survives turn
  resets on that socket.
- Normal onset requires 100 ms of sustained energy and barge-in requires 160 ms.
  The preceding 200 ms and candidate speech are preserved for STT. Idle and
  calibration frames no longer enter the STT audio queue. A short spike cannot
  reset the agent; each repeated interruption needs a fresh sustained onset.
- The utterance cap counts actual buffered samples, including pre-roll and
  intervening pauses. A large incoming frame cannot grow the captured utterance
  beyond the cap. Completed bursts below the minimum speech duration are audited
  and reset without invoking the planner.
- The browser enables additive audio feedback. Server levels and detection state
  update at most every 250 ms; the existing local meter remains available.
  Actionable hints distinguish missing frames, no detected speech, excessive
  noise, stopped audio and too-short input. Timeout hints are audited without
  creating customer turns. Stable conditions do not produce a hint every tick;
  renewed capture or usable speech rearms the feedback.
- A feedback-enabled disconnect exposed a cancellation race in cleanup. Cleanup
  is now shielded from ASGI cancellation until the owned tasks and STT stream
  finish, before socket ownership can be released. This uses the concurrency
  runtime already required by FastAPI; dependency files were not changed.

The 600 ms silence/quiet endpoint windows and 300 ms minimum speech duration were
not reduced. The fixed-threshold mode remains available explicitly. Tests that
pin that mode now request it in their configuration; their assertions remain.
The original single-frame barge-in fixtures now send sustained audio before
expecting interruption and still assert confirmation/cancellation behavior.

## Measured results

`scripts/vad_probe.py` was run before and after changing the detector. It generates
20 ms PCM16 frames at 16 kHz, with a one-second ambient prelude, an 800 ms
speech-level sine signal and an 800 ms tail. Ambient noise uses seeded Gaussian
samples. These measurements use audio-sample time, not wall-clock recognition or
provider latency. No STT, TTS, LLM or human speech is involved.

| Case | Before A2 | After A2 |
|---|---|---|
| Quiet signal: RMS 220 over RMS 20 ambient | No onset or endpoint | 100 ms onset; 600 ms endpoint |
| Normal signal: RMS 1600 over RMS 20 ambient | 20 ms onset; 600 ms endpoint | 100 ms onset; 600 ms endpoint |
| Signal RMS 4000 following RMS 800 ambient | False onset during ambient; no signal endpoint | 100 ms onset; 600 ms endpoint; no false onset |
| RMS 800 ambient only | One false onset | Zero false onsets |
| Intermittent signal with a 1000 ms cap | 1400 ms buffered | Exactly 1000 ms buffered |

In the cap case the new capture ends 800 ms after signal onset because its 200 ms
pre-roll also counts toward the 1000 ms cap. The normal onset deliberately trades
80 ms of the old single-frame response for sustained-energy rejection of spikes.
Unit and transport tests additionally exercise gradual room changes, very loud
noise, short bursts, repeated barge-in, initial calibration, bounded pre-roll,
preserved onset PCM, missing/stopped input and disconnect cleanup.

No new live `first_audio_ms` result is claimed. Speech-recognition accuracy,
microphone onset, acoustic echo and real provider/handset latency remain unmeasured.

## Commands and tests

Commands ran from `backend`, using the existing virtual environment.

| Command | Actual result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider` | Before A2: **167 passed, 1 warning in 53.27s**. Final: **198 passed, 1 warning in 60.54s** |
| `.\.venv\Scripts\python.exe -m pytest tests/test_vad.py tests/test_realtime_a2.py -q -o addopts= -p no:cacheprovider --tb=short` | Initially **24 failed, 2 passed**, including missing configuration/harness support. Final targeted run: **31 passed, 1 warning** |
| `.\.venv\Scripts\python.exe -m pytest tests/test_realtime_a2.py -k delayed_stream_close -q -o addopts= -p no:cacheprovider --tb=short` | **1 failed** before shielding cleanup; passes in the final full run |
| `node --test --test-reporter=tap tests/voice_browser.test.cjs` | Initially **9 passed, 3 failed** before UI feedback changes; final **12 passed, 0 failed** |
| `$env:PYTHONPATH = (Get-Location).Path; .\.venv\Scripts\python.exe scripts/vad_probe.py` | Before/after results in the table above; no files or provider requests generated |
| `$env:PYTHONPATH = (Get-Location).Path; .\.venv\Scripts\python.exe ../evals/runner.py` | **12/12 deterministic scenarios passed**; in-memory database and scripted plans |
| `.\.venv\Scripts\ruff.exe check app/realtime.py app/config.py tests/test_vad.py tests/test_realtime_a2.py tests/test_realtime_a1.py tests/test_realtime.py scripts/vad_probe.py --no-cache` | **All checks passed** after import cleanup/formatting |
| `git diff --check` | Passed; existing Git LF/CRLF conversion warnings remain |

An intermediate combined run reported **1 failed, 70 passed**: the existing
streaming-path test observed `stream.closed == 0` after disconnect. The delayed
close regression above reproduced it reliably and the cleanup repair fixed it.
No assertion was removed or weakened. The remaining Python warning is the
existing Starlette TestClient/httpx deprecation.

## Files changed in A2

- `backend/app/realtime.py`: detector, pre-roll, cap, sustained interruption,
  feedback, audits and cancellation-safe feedback/STT cleanup.
- `backend/app/config.py`, `backend/.env.example`: detector/feedback settings.
  The actual local `.env` was not read directly or changed.
- `backend/static/index.html`: microphone status line and feedback handling;
  existing page layout and playback-generation protocol retained.
- `backend/tests/test_vad.py`, `backend/tests/test_realtime_a2.py`: 31 new Python
  cases, including settings propagation and delayed disconnect cleanup.
- `backend/tests/test_realtime.py`, `backend/tests/test_realtime_a1.py`: explicit
  fixed-mode fixtures, configurable harness and sustained barge-in input.
- `backend/tests/voice_browser.test.cjs`: three new UI feedback regressions.
- `backend/scripts/vad_probe.py`: reproducible offline PCM measurements.
- `README.md`, `VOICE_REPAIR_ANALYSIS.md`, `A2_REVIEW.md`: behavior, commands,
  authorization status and review record. `A1_REVIEW.md` stays historical.

## Limits and review

Keep quiet briefly during the initial calibration hint. Speech covering the whole
calibration interval may be learned as background noise and need repeating.
Energy detection cannot reliably separate every sudden environmental noise or
speaker echo from speech. Before calibration, barge-in uses the provisional
configured threshold and may miss a quiet interruption. Adaptive calibration is
not automatic microphone gain control.

The tests use generated audio and scripted speech/model components; local
WebSocket tests do not establish real Prisma accuracy. The browser checks use
browser API fakes. No live matrix, real microphone, room-noise recording or phone
test was run. A1's single-process socket-ownership and abandoned-call limits
remain. Safe contextual wording and language mirroring are the next backend stage.

Existing `.env` files need no new settings for A2 defaults. One Gnani key remains
sufficient for the single-call demo design; A2 does not require extra keys.
