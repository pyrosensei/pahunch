# Pahunch evaluations

Four suites, from cheapest to most realistic:

| Suite | Command | Network | What it proves |
|---|---|---|---|
| Unit and integration tests | `cd backend && uv run pytest` | none | Policy, state machine, planner gates, voice socket, streaming STT against fakes |
| Deterministic call scenarios | `cd backend && uv run python ../evals/runner.py` | none | End-to-end call flows with scripted plans and no LLM |
| Live planner cases | `cd backend && uv run python ../evals/planner_runner.py` | NVIDIA NIM | The model returns schema-valid decisions for fixed Hinglish turns |
| Live voice matrix | `matrix_runner.py` (below) | Gnani STT/TTS + NVIDIA NIM | The real WebSocket voice path, policy, commit, and audit trail for T01–T22 |

## Live voice matrix

The matrix drives the actual FastAPI server over HTTP and WebSocket. It synthesises every customer turn with Gnani Timbre, streams it as 16 kHz PCM over the voice socket, reads the planner and policy decisions from SQLite, commits through the real endpoint, and writes a report.

### Run it

```bash
# terminal 1 — API server on a disposable database
cd backend
DATABASE_URL=sqlite:///./matrix.db uv run uvicorn app.main:app --port 8000

# terminal 2 — full matrix
cd backend
DATABASE_URL=sqlite:///./matrix.db uv run python ../evals/matrix_runner.py

# or a subset
DATABASE_URL=sqlite:///./matrix.db uv run python ../evals/matrix_runner.py --only T01,T16
```

`--base-url` points the runner at another server (default `http://127.0.0.1:8000`).

The runner deletes all rows in the target SQLite database before it starts. Point it only at a disposable database, never at a database you want to keep. Each scenario gets its own synthetic order (IDs 9101–9118 and 9120–9122) and its own call session, so scenarios cannot contaminate each other through cooldowns or session state.

### What it measures

Per scenario: every turn's transcript, planner action, confidence, policy verdict, timings, agent message, audio presence, and the full audit trail; final session state; proposals; commits; idempotent replay behaviour; and the stored courier card.

Across the matrix: live pass count, policy violations that reached a commit, agent messages that claim success before a commit, non-adversarial task completion, and P50/P95 for STT, planner, TTS, server turn time, and client-observed stop-to-turn time.

Reports land in `evals/reports/matrix-<UTC timestamp>.json` plus `matrix-latest.json` and `matrix-latest.md`. The Markdown file is the publishable summary. Raw provider usage (TTS characters, planner tokens, audio bytes) is included without pricing.

### Honest limits

- Customer turns are scripted text synthesised with Gnani Timbre, not a human speaker. There is no telephone codec, carrier jitter, or real background street audio; T15 mixes white Gaussian noise at 8 dB SNR.
- Prisma streaming STT returns no confidence and no partials. The live path relies on the explicit-confirmation gate before commit; low-confidence rejection is covered only by deterministic tests.
- T19 is deterministic-only. This architecture commits through local policy revalidation and a SQLite write, not a remote tool, so its failure semantics live in the test suite.
- Each scenario uses a fresh fixture order in a throwaway database. Production cooldowns and order lifecycle edges are exercised by unit tests.
- Planner calls time out at 12 s. NVIDIA NIM latency for the structured planner schema was measured at 0.7–7 s for the same prompt, so a stalled provider shows up as an `LLM_TIMEOUT` degraded turn and a failed scenario; that is provider variance, not a policy outcome.
- The matrix measures the current configuration. Changing the planner model, prompts, VAD thresholds, or TTS settings invalidates comparisons with older reports.
