# Stage D review

31 October 2026. Implemented the synthetic SwiftCart shop, operator dashboard and
approved additive APIs. Prior A1/A2/B/C changes are preserved, uncommitted, on
`feat/voice-latency-and-demo`. The user also requested a Gnani permission email;
`GNANI_PERMISSION_EMAIL.md` is a draft only and was not sent.

## Behavior and changed files

| Files | Change |
|---|---|
| `backend/app/demo.py` (new) | Fixed-choice checkout, deliberate/idempotent readiness, bounded call summaries, transcript cursor and dialer-event APIs |
| `backend/app/main.py` | Router registration; root/dashboard/shop pages; browser voice retained at `/index.html` |
| `backend/static/shop.html`, `shop.js`, `dashboard.html`, `dashboard.js`, `demo.css` (new) | Minimal local shop and polling dashboard with safe text rendering and visible failure/staleness feedback |
| `backend/static/index.html` | Navigation to shop/dashboard |
| `backend/tests/test_demo.py`, `backend/tests/demo_browser.test.cjs` (new) | API flow, privacy/bounds, checkout readiness, rendering and failure regressions |
| `README.md`, `VOICE_REPAIR_ANALYSIS.md`, this report | Behavior, test results, authorization and limits |

Checkout accepts COD/prepaid, a fixed parcel type and an OTP requirement. The
server creates fictional SwiftCart delivery details and prices. Personal fields
are rejected. Orders start PENDING, with a two-hour window starting 15 minutes
after checkout. Readiness cannot rewrite historical seed orders. New order events
reuse the existing order-scoped audit table, with no database column changes.

The dashboard shows current scheduler reasons, active/completed calls, transcript,
language, model/template wording source, fallback reason, action, proposal,
policy verdict/alternatives, explicit confirmation, courier card and audit data.
It polls every two seconds and marks stale data after refresh errors. Phone
dispatch/consent fields were added during the following E work.

## Commands and results

Commands ran from `backend`, using the installed environment:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider
```

Before edits: **301 passed, 1 warning in 64.59s**.
After D implementation, before E: **310 passed, 1 warning in 80.16s**.
The final combined suite, including one additional D transcript regression and
the E cases, passed **352 tests, 1 warning in 81.61s**. The warning is the existing
Starlette TestClient/httpx deprecation.

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider tests/test_demo.py --tb=short
node --test --test-reporter=tap tests/demo_browser.test.cjs tests/voice_browser.test.cjs
```

The initial **9 D API cases failed** with the old missing endpoints. They then
passed. A tenth case verifies redaction of legacy transcript text and cursor
behavior; it passes in the final suite. Three initial browser tests failed
because the new scripts did not exist. One subsequent dashboard test awaited a
second refresh while initialization was already running; the test was corrected
to await the initial refresh promise. Assertions were retained. Final combined
Node result: **23 passed, 0 failed** (18 existing voice cases, 3 D cases and 2
additional dashboard error/phone-gate cases).

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe ../evals/runner.py
.\.venv\Scripts\ruff.exe check app/demo.py tests/test_demo.py app/main.py --no-cache
```

Deterministic evaluation: **12/12 passed**, using scripted proposals and in-memory
SQLite. The full changed-file Ruff check also passed; exact command is in
`E_REVIEW.md`. `git diff --check` passed with Git line-ending/global-ignore
permission warnings during inspection.

## Exercised flow and limits

The HTTP test creates a COD order, marks it ready, runs the real local scheduler,
connects its browser session, submits a scripted model proposal through the real
planner/policy route, confirms with an explicit yes, commits through the existing
guard and reads the resulting courier card. Both COD and prepaid checkout and
the server-controlled high-value price are tested. This is not speech recognition.

Browser tests run the real page scripts against DOM and fetch doubles. A temporary
local preview server was started with synthetic data and no speech provider;
computer-use navigation failed because the browser host and native pipe were
unavailable. The preview process was stopped. Real visual/responsive/accessibility
inspection remains unverified; no on-device or screenshot QA is claimed.

The demo is local and has no added authentication. Call summaries and order audit
views are bounded; existing session audit history retains its original endpoint.
It is not a production storefront, payment flow or public deployment. No existing
database, actual `.env`, credentials, dependencies or CI configuration was changed.
No live provider request, phone call, push or publication was performed.
