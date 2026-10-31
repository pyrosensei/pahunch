# Stage C review

31 October 2026. Implemented the next authorized stage after B: automatic local
browser scheduling, shared admission, retry controls, recovery and audit state.
The Twilio screenshot was treated as account information. No telephone integration
or live call was performed. Work remains uncommitted on
`feat/voice-latency-and-demo`; prior A1/A2/B changes are preserved.

## Result

- Startup sweeps eligible orders without an operator request, then runs one
  background task. Shutdown signals and awaits it. Worker errors are logged and
  exposed in the additive read-only `/api/dialer/status` endpoint.
- Automatic eligibility requires OUT_FOR_DELIVERY, complete metadata, no opt-out,
  a prior failed delivery/COD/OTP/high-value risk, and a window beginning within
  60 minutes or already ongoing but unexpired.
- Manual HTTP starts and sweeps share fresh database reads and one process lock.
  Capacity is one active demo call across orders. Attempts are counted from call
  rows, capped at three, with a one-hour gap from the latest end/start and any
  later order completion timestamp. Delivery failures are a separate count.
- Resolved history blocks recall. NO_ANSWER and UNREACHABLE can retry after
  eligibility checks. TOOL_FAILURE requires an explicit transient record; an
  unknown failure is blocked.
- Browser-only attempts have an unanswered timeout, reconnect grace and abandoned
  connected-session timeout. Recovery records outcomes and preserves history.
  Lifecycle mutations share the admission lock so recovery cannot overwrite an
  answer, customer turn or commit in flight.
- STARTED, observed disposition changes and skip-reason changes are stored in
  order-scoped events. Repeated unchanged sweeps create no duplicate audit events.
  Current skip state is held separately with the time of its last evaluation.
- The existing browser attaches to an automatic CALLING session by completing
  connection/context steps first. Expired/failed connections cannot open voice.
  There is no layout redesign.

## Files changed in this stage

| Files | Purpose |
|---|---|
| `backend/app/dialer.py` (new) | Clock-injected sweep, recovery, worker lifecycle, error/status reporting and audit deduplication |
| `backend/app/calls.py` | Shared admission and mutation serialization, retry/capacity rules, browser presence and explicit failure dispositions |
| `backend/app/models.py` | New CallRuntime, DialerOrderState and DialerEvent tables; existing columns unchanged |
| `backend/app/state_machine.py` | Calling attempts may record TOOL_FAILURE |
| `backend/app/main.py` | Lifespan scheduling/shutdown, status endpoint, browser attachment/detachment tracking |
| `backend/app/config.py`, `backend/.env.example` | Scheduler enablement, sweep interval and recovery deadlines |
| `backend/static/index.html` | Scheduled-session connection/context handling and error checks |
| `backend/tests/test_dialer.py` (new) | 46 scheduler/admission/clock/concurrency/recovery/status tests using temporary databases |
| `backend/tests/voice_browser.test.cjs` | Four browser attachment regressions |
| `README.md`, `VOICE_REPAIR_ANALYSIS.md`, `C_REVIEW.md` | Behavior, limits, authorization scope and review record |

No dependencies or CI configuration changed. No credentials were displayed or
edited. No existing database was modified by the verification commands; tests
used temporary files or in-memory SQLite. No commit or push was made.

## Commands and results

From `backend`, baseline and final full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider
```

Baseline: **255 passed, 1 warning in 60.83s**. Final: **301 passed, 1 warning
in 70.88s**, including 46 new Python cases. The warning is the existing
Starlette TestClient/httpx deprecation.

New regressions were exercised before fixes:

- Initial admission tests: **8 failed**. They reproduced duplicate concurrent
  attempts and missing capacity/attempt/resolved-history checks.
- Scheduler tests before the module existed: **13 passed, 18 setup errors**.
  After implementation, **25 passed, 6 failed**. Five failures were in a new
  boundary fixture that incorrectly replaced an already-UTC timestamp's zone;
  it was corrected to use the existing policy conversion. The sixth exposed
  a missing injected clock in no-answer timestamps. The selection then passed
  **31 tests**.
- Startup injection test: **1 failed, 1 passed**, before lifespan wiring.
- Answer-versus-expiry test: **1 failed, 1 passed**, before lifecycle serialization.
- A detached legacy CREATED session initially hit an illegal recovery transition:
  **1 failed, 1 passed**. Recovery now takes the legal CALLING-to-NO_ANSWER path.
- Browser attachment tests: **15 passed, 3 failed** before the UI connection fix;
  their final result is **18 passed, 0 failed**.

Targeted scheduler/call/API/planner run after serialization:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider tests/test_dialer.py tests/test_calls.py tests/test_call_api.py tests/test_planner.py --tb=short
```

**113 passed, 1 warning** at that point (before the extra legacy recovery case).
No existing test assertions were weakened.

Other checks:

```powershell
node --test --test-reporter=tap tests/voice_browser.test.cjs
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe ../evals/runner.py
.\.venv\Scripts\ruff.exe format app/dialer.py tests/test_dialer.py
.\.venv\Scripts\ruff.exe check app/dialer.py tests/test_dialer.py app/models.py app/calls.py app/config.py app/main.py app/state_machine.py --no-cache
```

Browser: **18/18 passed**, using browser API/DOM fakes. Deterministic evaluation:
**12/12 passed**, with scripted proposals and in-memory SQLite. Ruff: **all checks
passed** after correcting exception logging and an import alias.
`git diff --check` passed from the repository root. Git emitted line-ending and
global-ignore permission warnings during inspection; no tracked whitespace
errors were reported.

## Measurements and remaining limits

- Injected-clock cases check exactly 60 minutes before a window, window end,
  3,599/3,600/3,601-second retry gaps, a 119/120-second unanswered deadline and
  29/30-second reconnect grace. These are deterministic boundary checks, not
  measured real-world delivery or carrier timings.
- Repeated unchanged sweeps preserve event count while updating current-state
  timestamps. Concurrent manual/manual and HTTP/sweep tests create one attempt.
  A controlled answer/expiry interleaving prevents stale outcome overwrite.
- A lifespan test creates a CALLING browser session before any HTTP request,
  with no customer confirmation, and verifies the background task stops on exit.
  Session creation is not evidence that a phone rang or a provider accepted it.
- Use one app process/worker and one active demo call. There is no distributed
  lock, multi-worker ownership or throughput claim based on API-key count.
- Defaults are a 5-second sweep, 120-second unanswered deadline, 30-second
  disconnect grace and 300-second abandoned-session deadline. Expiry runs on the
  next successful sweep. A live socket prevents abandonment expiry; this is not
  a maximum-duration limit for an open browser call.
- Disposition and current skip snapshots update on sweeps; existing per-session
  state-change audits remain available between sweeps. Disabling the scheduler
  also disables its timeout recovery, while admission limits still apply.
- Fixed historical seed windows are unchanged. Relative-date demo orders and the
  operator dashboard remain Stage D. The new status endpoint is local and has
  no added authentication; public deployment is outside this pass.
- No live NIM/Gnani request, Twilio request, handset test or new first_audio_ms
  measurement was made. The 30-day account allowances shown in the screenshot
  were not independently checked against account settings or destination rules.
