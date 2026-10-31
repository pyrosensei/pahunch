# Feature branches and commits

31 October 2026. The accumulated repair work is organized as a stack from
`a95fae3`, the original tip of `feat/voice-latency-and-demo`. Each branch below
includes the preceding row. They are dependent review branches, not independent
alternatives. `main` remains at `a543a60`.

| Branch | Own commits | Scope | Exported Python suite |
|---|---|---|---|
| `chore/voice-repair-guidance` | `50285b6` | Approved scope, repository safeguards and scratch exclusion | Documentation only |
| `feat/runtime-contracts` | `d61f502` | Shared runtime records, settings and readiness contracts | 149 passed |
| `feat/conversation-safety` | `7612c77` | Server-controlled facts, contextual replies, language and confirmation safety | 204 passed |
| `feat/call-scheduling` | `67b1ea8` | Shared admission, eligible-order scheduling, recovery and audit changes | 250 passed |
| `fix/voice-session-runtime` | `7f0117b`, `6c9f4c8` | Separate detector/pre-roll/cap and responsive-session/playback commits | 272, then 309 passed |
| `feat/guarded-phone-adapter` | `4c637b9` | Session approval, consent, signed callbacks and bound media | 350 passed |
| `feat/synthetic-shop-dashboard` | `19c831d` | Synthetic checkout/readiness and operator views | 360 passed |
| `docs/synthetic-test-review` | Branch tip | Current README, earlier synthetic test review and this map | Documentation only; same source/test tree as previous row |

The existing integration branch `feat/voice-latency-and-demo` is fast-forwarded
to the final stack tip using `--ff-only`. All feature refs are retained. No
commits were amended, branches deleted, remote refs changed or safety hooks
bypassed. The historical stage reports retain their original observations and
authorization context; this map records the later commit organization.

## Validation during organization

Tests ran against isolated exports of the staged trees, not the full dirty
working directory. The existing virtual environment was reused without installing
dependencies. Snapshot directories and pytest logs stay under ignored
`backend/.artifacts/commit-organization/`.

The final code export was checked from the repository root with:

```powershell
.\backend\.venv\Scripts\python.exe backend/.artifacts/organize_commits.py validate 7
```

The local helper exports the index tree and runs the existing venv interpreter
with `-m pytest -q -o addopts= -p no:cacheprovider` and a unique workspace
`--basetemp`, from the export's `backend` directory. Final result:
**360 passed, 1 warning in 85.46 s**. Each earlier source snapshot also passed
its complete collected suite as listed above. The warning is the existing
Starlette TestClient/httpx deprecation.

From the final exported `backend` directory:

- `node --test --test-reporter=tap tests/voice_browser.test.cjs tests/demo_browser.test.cjs`:
  **23 passed, 0 failed** against browser/DOM fakes.
- The existing venv interpreter running `..\evals\runner.py`:
  **12/12 passed**, using scripted plans and an in-memory database.

From the working `backend` directory,
`.\.venv\Scripts\ruff.exe check app tests --no-cache` passed. Staged diffs were
reviewed before every commit, and `git diff --cached --check` passed.

The first baseline command without a workspace `--basetemp` reported
**283 passed and 77 setup errors**: Windows denied access to the shared
`Temp/pytest-of-sweta` directory. Repeating the command with
`--basetemp=.artifacts/pytest-git-baseline-20261031` passed **360 tests, 1 warning
in 69.46 s**. No assertions or product behavior were changed for that issue.

The original 89 source/document files are preserved byte for byte. This map is
the only new project content from organization; intermediate index snapshots
split overlapping files without rewriting the final implementation. No actual
`.env`, database, audio, generated provider output, cache or scratch artifact is
included in the new commits. No provider or handset test was rerun for this task.
