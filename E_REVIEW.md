# Stage E review: local adapter and mocked validation

31 October 2026. The user authorized locally implementing and testing the Twilio
adapter after D. They reported that credentials were entered but console/account
verification was unclear and Gnani had not been contacted. Live verification is
therefore pending. No live call, tunnel, Twilio account request or provider speech
test was performed. `GNANI_PERMISSION_EMAIL.md` was drafted, not sent.

## Implemented

- Session-specific dispatch permission plus closed-by-default verification gates
  for console-call delivery, streaming access and Gnani approval. Credentials
  alone cannot trigger a call. Only the configured owner handset can be a
  recipient; there is no arbitrary-recipient dial API.
- A persisted REQUESTING/ACCEPTED/REJECTED/UNKNOWN binding reserves one logical
  attempt before the provider POST. Timeout, malformed success and server-error
  ambiguity do not trigger another POST. Pending/ambiguous provider calls are
  excluded from browser timeout recovery and keep capacity reserved. Missing
  callbacks can require operator reconciliation. Only explicit 429 rejection is
  classified as a retryable dispatch failure, still subject to a new approval.
- Signed answer/status/consent callbacks, bounded form input, account/call binding,
  ordered callback handling and terminal-state protection. Signature validation
  includes all received form fields and handles duplicate parameter values.
- DTMF consent to speak before opening a media stream. Pressing 1 does not confirm
  a delivery plan. Refused/ambiguous consent ends in human-review disposition;
  digits are not retained in audits or customer text.
- Signed WSS handshake, consent and account/call/stream/format checks, duplicate
  ownership exclusion and one stream per approved call. The browser fallback
  cannot attach to the same phone-transport session.
- A phone socket adapter reuses the repaired realtime engine. It translates
  mu-law/8000 to PCM16 for existing VAD/STT, uses native mu-law TTS requests with
  language propagation and blocking fallback, and sends header-free mu-law
  media. Playback marks map to final-drain controls; clear flushes interrupted
  generations and late marks cannot resume cancelled audio. No `audioop`, codec
  library or resampling dependency was introduced.
- A confirmed phone plan commits through the existing policy revalidation and
  explicit-confirmation gate. Final playback ends the stream; the TwiML then
  hangs up. Provider completion cannot overwrite COMMITTED. No audio recording
  request or audio-file persistence was added.
- Readiness and dashboard fields disclose booleans/state only. Provider error
  bodies, credentials, handset values and public URL are excluded from audits
  and call summaries. Real verification reads credentials through Settings only.

## Files changed in E

| Files | Purpose |
|---|---|
| `backend/app/telephony.py` (new) | Readiness/approval gates, dispatch reservation, safe error handling, signatures and HTTP callbacks |
| `backend/app/phone_media.py` (new) | Bound phone socket, mark/clear/media handling, shared voice-engine orchestration and guarded commit |
| `backend/app/phone_audio.py` (new) | G.711 mu-law codec and native TTS adaptation, including empty-audio rejection |
| `backend/app/models.py` | New TwilioBinding table; existing table columns unchanged |
| `backend/app/config.py`, `backend/.env.example` | Verification booleans and approval for one existing session; all gates closed by default |
| `backend/app/main.py` | Provider lifetime, shared ownership, routes and 8 kHz speech configuration |
| `backend/app/dialer.py` | Approved dispatch outside admission lock; reservations survive ambiguous outcomes and browser deadlines |
| `backend/app/gnani.py` | Additive native container option for streaming/blocking TTS; browser defaults unchanged |
| `backend/app/demo.py`, `backend/static/dashboard.js` | Safe dispatch/consent summaries and human-readable pending setup |
| `backend/tests/test_telephony.py`, `backend/tests/test_phone_media.py` (new) | 41 phone transport/audio/protocol/security cases |
| `backend/tests/demo_browser.test.cjs` | Additional dashboard readiness and stale-data cases |
| `README.md`, `VOICE_REPAIR_ANALYSIS.md`, this report | Current behavior, limits and verification record |

Prior stages remain uncommitted on `feat/voice-latency-and-demo`. No actual `.env`
or existing database was edited. No dependencies, CI, commits or remote state
were changed.

## Commands and results

From `backend`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider
```

Pre-E full suite: **310 passed, 1 warning in 80.16s**. Final combined result:
**352 passed, 1 warning in 81.61s**. This includes 10 D tests and 41 E tests added
to the 301-test baseline. Warning: existing Starlette TestClient/httpx deprecation.

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider tests/test_demo.py tests/test_telephony.py tests/test_phone_media.py --tb=short
```

The selection passed **49 tests** before the two final empty-audio regressions
were added; those two pass in the final full suite. They initially failed before
the proxy rejected empty acknowledgement/blocking synthesis.

Earlier regressions and fixes were exercised openly:

- Initial phone test run: **9 failed, 8 setup errors**, before the modules existed.
- Callback-route injection: **8 setup errors**, before app wiring.
- Initial phone media tests: **5 failed**, before the adapter existed.
- First route integration run had **3 fixture errors** because the imported phone
  fixture needed its engine fixture too; fixture registration was corrected.
- The synthetic phone-flow test initially reported **1 failed, 8 passed**. Its
  frame assertion observed grouped STT input calls, whereas the actual Prisma
  client frames those groups into 1,024-byte wire messages. The fixture now uses
  the real Prisma framing method with a mocked wire; the exact-size assertion is
  retained and passes. Recognition text remains scripted.
- Two malformed-media cases initially raised raw AttributeError/TypeError. They
  now fail with an explicit, audited protocol failure.
- Two empty-synthesis tests initially failed. Empty audio is now rejected before
  cache insertion or first-audio accounting.

```powershell
node --test --test-reporter=tap tests/demo_browser.test.cjs tests/voice_browser.test.cjs
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe ../evals/runner.py
.\.venv\Scripts\ruff.exe check app/demo.py app/telephony.py app/phone_audio.py app/phone_media.py app/dialer.py app/main.py app/gnani.py app/config.py app/models.py tests/test_demo.py tests/test_phone_media.py tests/test_telephony.py --no-cache
```

Node: **23/23 passed** against browser/DOM doubles. Deterministic evaluation:
**12/12 passed**, scripted and in-memory. Ruff: **all checks passed** after import
sorting and context-manager formatting fixes. `git diff --check` passed; Git
emitted line-ending and global-ignore permission warnings during inspection.

## Measured checks and remaining live gates

- Six G.711 decode/encode reference cases and all 256 mu-law byte values are
  checked, with the negative-zero byte canonicalized to positive zero.
- Mocked HTTP verifies mu-law/8000 requests and language propagation. Protocol
  tests verify bounded 1,024-byte PCM input framing, output media, final playback
  marks, clear, late-mark rejection, signature tampering, consent, malformed
  payloads, callback duplication/reordering and terminal fences.
- The synthetic WebSocket test reaches PENDING_CONFIRMATION before the scripted
  yes, then COMMITTED afterward through real planner/policy/commit code. Audio
  is synthetic, speech recognition responses are scripted, and Twilio is mocked.
  It does not prove a handset rang, heard speech or recognized a real speaker.
- Credential presence was checked through `app.config.settings`: all four
  account/token/from/handset fields are present; the public endpoint is absent.
  Values were not printed. Verification flags and session approval remain closed;
  this does not assert that the user's console setup failed.
- Gnani permission, console-call delivery, upgraded/stream-capable Twilio account,
  public HTTPS/WSS ingress and immediate permission for one live call are still
  required. Current trial rules block `<Stream>`; see
  [Twilio's trial reference](https://www.twilio.com/docs/usage/trials/try-out-voice#blocked-verbs).
- No tunnel was configured. Before a live test, ingress must expose only
  `/twilio/*`, rather than the unauthenticated local shop/dashboard/API surface.
  One app process and one active call remain correctness limits.
- Missing callbacks or a create-call timeout without a provider SID deliberately
  leave capacity reserved. Automatic reconciliation of a SID-less ambiguous
  attempt is not implemented; operator review is required. Do not delete its
  history or retry the POST to clear it.
- The signature implementation supports the exact configured form/WSS URLs. It
  is tested against an [official Twilio SDK vector](https://github.com/twilio/twilio-python/blob/main/tests/unit/test_request_validator.py).
  Twilio recommends its SDK validator in its
  [webhook security guidance](https://www.twilio.com/docs/usage/webhooks/webhooks-security);
  the SDK is not installed, and installing dependencies was not authorized.
  Proxy URL/port/trailing-slash behavior is not live-verified; mismatches fail
  closed. This narrow validator is not a claim of complete SDK compatibility.
- Native Timbre phone output, Prisma handset recognition, acoustic echo,
  language pronunciation, carrier delivery and new real `first_audio_ms` remain
  unmeasured. Existing README live-matrix numbers were not rerun.
- Browser visual inspection was blocked by an unavailable computer-use host.
  Functional DOM/HTTP checks passed; no visual or on-device claim is made.
