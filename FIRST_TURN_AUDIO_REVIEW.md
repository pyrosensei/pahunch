# First-turn audio review

31 October 2026. The previously accumulated nine commits were published to
GitHub on the integration branch and eight feature branches. This follow-up
works on `perf/first-turn-audio` and improves acknowledgement readiness.

## Change

The opening previously prepared only a Hinglish acknowledgement. English and
Hindi acknowledgement audio was prepared after that language's first response,
so it could not fill the planner wait on the first turn.

After opening synthesis, the socket now prepares all three supported languages
at its selected output rate. A turn uses only completed audio for its matching
language. It cancels and awaits unfinished preparation after STT finishes and
before planning, so optional work cannot keep the shared speech slot ahead of
the reply. Cancellation is audited, cancelled work can restart after the reply,
and disconnect owns and awaits active/queued preparation.

`VOICE_TURN` audit payloads now include `first_response_audio_ms`, measured at the
first substantive reply chunk. Existing `first_audio_ms` still measures the
first chunk including an acknowledgement. The browser protocol and typed response
schemas are unchanged. Neither metric includes VAD endpoint delay, physical
playback or carrier delay.

Policy, explicit confirmation, OTP protections, language matching and VAD
constants are unchanged. This change improves listening feedback, not model
inference speed or the substantive answer's latency.

## Measurements

An exploratory real-TTS comparison tested whole replies versus separate
sentences. It was not adopted: the benefit was inconsistent and Hindi complete
audio took longer in that sample. No sentence splitting was added to production.

| Language | Whole first audio / completion, ms | Sentences first audio / completion, ms |
|---|---|---|
| English | 743 / 2656 | 343 / 2709 |
| Hindi | 324 / 2217 | 346 / 6677 |
| Hinglish | 398 / 2953 | 379 / 3291 |

These are individual paired requests, not percentiles or controlled statistical
comparisons. The scratch script and JSON stay under ignored `.artifacts/`.

The first post-change end-to-end run passed **5/5 scenarios** with real Gnani
and NVIDIA NIM: English offer/confirmation, Hindi offer/confirmation, greeting,
native 8 kHz phone-format offer/confirmation, and an offer followed by a changed
security-drop instruction. Three expected plans committed; the changed
instruction remained unconfirmed and was rejected by COD policy. First audio
ranged from **741 to 1497 ms**, including acknowledgements. No audio errors were
reported. Twilio transport and playback were mocked.

A final two-scenario run exercised the additional response-audio audit:

| Turn | First audio, ms | First substantive response audio, ms | Acknowledgement |
|---|---|---|---|
| Browser English proposal | 1088 | 3547 | Yes |
| Browser English confirmation | 690 | 690 | No |
| Mocked phone English proposal | 1192 | 3618 | Yes |
| Mocked phone English confirmation | 782 | 782 | No |

Both scenarios committed correctly. HTTP observations were customer TTS 4x200,
server speech 12x200 and planner 2x200, with no other HTTP statuses recorded.
Only the primary Gnani key was used. This does not establish concurrent-call
capacity or account quotas. No real Twilio request or handset call occurred.
The final substantive responses still took about 3.5–3.6 seconds to begin;
sub-second response performance is not claimed.

The local records are
`backend/.artifacts/synthetic-20261031T175236Z/results.json` (five scenarios) and
`backend/.artifacts/synthetic-20261031T175531Z/results.json` (final two scenarios).
The latter isolated database contains the separate `VOICE_TURN` timing values.
No existing database was reset and no actual `.env` field was changed.

## Validation

Commands ran from `backend` with the existing virtual environment. Workspace
`--basetemp` avoids the previously observed Windows shared-temp permission issue.

- Baseline: `.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider --basetemp=.artifacts/pytest-latency-baseline-20261101`:
  **360 passed, 1 warning in 88.13 s**.
- New regressions before the implementation: `.\.venv\Scripts\python.exe -m pytest tests/test_voice_latency.py -q -o addopts= -p no:cacheprovider --basetemp=.artifacts/pytest-first-turn-red`:
  **7 failed, 1 warning in 8.69 s**, reproducing missing first-turn languages and
  blocked foreground speech. Two cleanup/retry cases were added afterward.
- First full post-change suite: **369 passed, 1 warning in 78.55 s**. The audit
  distinction was subsequently checked by the nine targeted latency cases,
  which passed in 5.14 s.
- Final release command: `.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider --basetemp=.artifacts/pytest-first-turn-release --tb=short`:
  **369 passed, 1 warning in 95.72 s**, after the final source and test edits.
- `node --test --test-reporter=tap tests/voice_browser.test.cjs tests/demo_browser.test.cjs`:
  **23 passed, 0 failed** against browser/DOM fakes.
- `.\.venv\Scripts\python.exe ..\evals\runner.py`: **12/12 passed** with scripted
  plans and in-memory SQLite.
- `.\.venv\Scripts\ruff.exe check app tests --no-cache` and `git diff --check`:
  passed. An initial Ruff argument-shadowing error was corrected.
- Real-provider commands: `.\.venv\Scripts\python.exe .artifacts/latency_probe.py`,
  `.\.venv\Scripts\python.exe .artifacts/synthetic_eval.py english_direct,hindi_direct,phone_8000_loopback,greeting,changed_confirmation`,
  then `.\.venv\Scripts\python.exe .artifacts/synthetic_eval.py english_direct,phone_8000_loopback`.

Nine new parameter cases cover first English/Hindi acknowledgement at 8/16/48
kHz, foreground-slot release, warmup retry, disconnect cleanup and the audit
distinction. The warning remains Starlette's existing TestClient/httpx
deprecation; no dependency was installed.

## Files and limits

Changed: `backend/app/realtime.py`, `backend/app/voice.py`,
`backend/tests/test_voice_latency.py` (new), `README.md`, and this review.
Generated audio, databases, results and scratch scripts remain ignored.

Human speech, microphone/headset behavior, acoustic echo, visible browser QA,
carrier behavior and a real consented handset call remain unverified. The
historical 21-case live matrix was not rerun. Gnani's answer to the drafted
permission email and actual Twilio streaming/callback readiness are still
required for the later handset demo. Dependencies and UI layout were unchanged.
