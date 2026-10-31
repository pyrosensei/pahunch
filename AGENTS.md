# AGENTS.md

Instructions for any agent working in this repository. They apply to every file,
every task and every provider. If a request conflicts with a rule here, state the
conflict and stop; do not silently ignore the rule.

## 1. Read before you touch anything

- Read `README.md` and the files you are about to change before editing them.
- Do not describe code you have not read. If you are unsure what a function does,
  open it; guessing costs more time than reading.
- Before changing behaviour, find the tests that pin it and read them.

## 2. Secrets and credentials

- Never print, echo, paste into chat, or commit an API key, token, password, or
  any other credential. Not even partially, not even to "verify" it.
- Read credentials only through `backend/app/config.py` settings. When you must
  handle one in a script, refer to it by position or label, never by value.
- `.env` files and `*.db` are gitignored for a reason. Never commit them, and
  never add a rule that would un-ignore them.
- If you accidentally expose a secret, say so immediately so it can be rotated.
- Example files (`.env.example`, docs) may contain empty placeholders only.

## 3. External side effects require approval first

Ask before any action that is hard to undo or visible outside this machine:

- pushing to a remote, opening or closing a PR or issue, posting anywhere;
- deleting or force-resetting branches, `git reset --hard`, amending commits,
  force-pushing, `git clean -f`;
- installing or upgrading dependencies, changing CI configuration;
- placing real phone calls, sending messages, or touching a shared service;
- deleting files you did not create yourself.

Never use `--no-verify`, `--no-gpg-sign`, or any other bypass of a safety check
to make a failing step succeed. If a hook fails, fix the cause.

## 4. Scope discipline

- Change only what the task requires. No drive-by refactors, formatting sweeps,
  or "while I was here" cleanups.
- Do not add a feature, abstraction, helper, flag or compatibility shim that the
  task did not ask for. Three similar lines beat a premature abstraction.
- Do not leave half-finished features. If you cannot complete something, revert
  it and say so, or leave it clearly isolated and report it as incomplete.
- Delete unused code rather than renaming it or re-exporting it "just in case".
- Comments explain why, not what. Never write a multi-paragraph docstring or a
  comment that describes the change you are making in this task.

## 5. Git discipline

- Work on a feature branch. Do not commit directly to `main` unless asked.
- Commit in small, logical groups, one concern per commit. No bulk "WIP" commits.
- Use conventional commit prefixes: `feat:`, `fix:`, `perf:`, `test:`,
  `refactor:`, `chore:`, `docs:`.
- A commit message states why the change was made, not which files changed.
- Before every commit, check what is actually staged (`git diff --cached`).
- Never commit generated databases, logs, screenshots, model outputs, or caches.

## 6. Test discipline

- Keep the suite green. Run it before and after your change and report the count.
- Never weaken an assertion, delete a test, or change a fixture's meaning to make
  a failing test pass. If a test is genuinely wrong, say why and change it openly.
- Every new behaviour gets a test that fails without the change.
- Prefer tests that observe behaviour over tests that assert implementation.
- Do not claim a test was run unless you ran it in this session. Paste the real
  command and the real result, including failures.

## 7. Honesty in reporting

- Never fabricate numbers, benchmark results, evaluation scores, latency figures,
  token counts, or "verified on device" claims. Measure, or say it is unmeasured.
- Distinguish clearly between mocked and real components. A scripted customer turn
  is not speech recognition; say so wherever it matters.
- Do not present a hypothesis as a finding. Label inference as inference.
- When reporting results, include what is still broken or untested. A summary that
  omits known failures is a false report.
- No emojis anywhere: not in code, comments, commits, docs, or responses.

## 8. Product invariants for this project

These are not style preferences; breaking them breaks the product's core claim.

- **Policy is code, not a model.** The LLM proposes a plan; `backend/app/policy.py`
  decides whether it is legal. Never let a model response commit, approve, or
  bypass policy, and never remove a deterministic guard to make a demo look good.
- **No OTP collection.** The agent must never request, store, or speak an OTP.
  The existing safety guard is deliberate; do not relax it.
- **Synthetic data only.** No real customer names, addresses, or phone numbers.
  The merchant is the fictional `SwiftCart`; never introduce a real brand.
- **No real customer calls** without explicit approval and a working consent flow.
- **Confirmation must be explicit.** Do not broaden what counts as "yes" without
  tests proving ambiguous and negative utterances are still rejected.
- **Audit every decision.** If a branch can change a call's outcome, it must leave
  an auditable record of what it did and why.

## 9. Realtime and speech code

- Do not loosen VAD thresholds, silence windows, or timeouts to "fix" a symptom
  without measuring the effect. Tuning one constant can trade a missed utterance
  for a self-triggering loop, which is worse.
- A missed detection must be visible. Never let a failure mode produce silence
  with no error, no log and no hint to the user.
- Never swallow an exception. If you catch one, either handle it meaningfully or
  record it and propagate the failure state.
- Latency claims need a measured `first_audio_ms`, not an estimate.

## 10. Definition of done

A change is complete only when all of these hold:

- the requested behaviour works and has been exercised, not just written;
- tests pass, including new tests for the change;
- no unrelated files are modified;
- no secrets, generated data, or debug artifacts are committed;
- `README.md` is updated if documented behaviour changed;
- the report lists files changed, commands run, measured results, and limits.

## 11. When to stop and ask

Stop and ask instead of guessing when:

- two reasonable designs would take materially different directions;
- a requirement conflicts with a rule in this file;
- the change would remove a guard, alter a public API, or add a dependency;
- you would have to invent product behaviour that is not specified anywhere.

Ask the smallest useful question, with your recommended option stated first.
