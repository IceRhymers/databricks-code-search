---
name: autoplan
description: Drives one GitHub issue through this repo's full development loop — ralplan, sized execution, gates, PR, and a real review loop on the open PR — end to end
argument-hint: "[--yes] [--interactive] <issue-number>"
level: 4
---

<Purpose>
Autoplan takes a databricks-code-search GitHub issue number and drives it
through the repo's development pipeline without the operator having to manually
chain skills:

```
ralplan ──► GitHub issue (already exists) ──► sized execution (ralph/autopilot/team)
  ──► pre-PR gate + code-review loop ──► SPEC-drift check ──► PR (Closes #NNN)
  ──► post-PR review loop (real PR comments, real amending commits)
```

`/autoplan 13` is shorthand for: read issue #13, ralplan it, execute it at the
right size, run this repo's mandatory gates, open the PR against `master`, then
keep addressing review feedback on that PR until it's clean.
</Purpose>

<Use_When>
- User says `autoplan <N>`, `autoplan issue <N>`, or `/autoplan <N>`
- User wants a GitHub issue taken from plan to a review-ready PR in one pass
- The issue already exists (autoplan does not file issues — that's ralplan's
  job when run standalone; see `Do_Not_Use_When`)
</Use_When>

<Do_Not_Use_When>
- No issue number given, or the issue doesn't exist yet — run `ralplan`
  directly first to produce the issue, then `autoplan <N>`
- User wants only the plan, not execution — run `ralplan` directly and stop at
  the consensus plan
- User wants a single trivial fix with no ceremony — delegate directly to an
  `executor` agent instead
</Do_Not_Use_When>

<Why_This_Exists>
This repo's real workflow (see the merged PRs on `master`: consensus-approved
ralplan, phased execution, a pre-PR gate run, `Closes #NNN` PRs, then an
"Address review pass N" fix-commit loop) is consistent but manual. Autoplan
encodes the orchestration mechanics — which execution skill to invoke for a
given issue's size, which mandatory gates must be green before a PR exists, and
how the review loop posts and resolves real comments on the open PR — so the
loop runs the same way every time.

There is no AGENTS.md/CLAUDE.md in this repo; the mandatory gates live in the
`Makefile` and `.github/workflows/ci.yml`. This skill references those directly.
</Why_This_Exists>

<Repo_Facts>
Load-bearing facts this skill depends on (verify if the repo has moved on):
- **Base branch:** single `master`. There are no long-lived `integration/*`
  branches — every feature PR targets `master`. `origin/HEAD → origin/master`.
- **Branch naming:** feature branches are `feat/<slug>` or `issue/<N>-<slug>`;
  fixes use `fix/<slug>`. This skill creates `feat/<N>-<slug>` (or
  `fix/<N>-<slug>` for `bug`-labeled issues).
- **Stack:** Python 3.12, `uv` for env/deps, `ruff` (lint + format), `mypy`,
  `pytest` (markers: `unit`, `observability`, `integration`, `e2e`), Alembic
  migrations, FastMCP server (`app/`), Databricks Asset Bundle (`databricks.yml`).
- **Modules (the "affected area" set for sizing):** `app/search`, `app/query`,
  `app/db`, `app/alembic`, `app/main.py`, `indexer/`, `scripts/`, `resources/`.
- **Mandatory gates** (all must pass with fresh output before a PR):
  - `make lint` — `ruff check .` + `ruff format --check .` + `mypy app indexer`
  - `make test` — unit + observability tests (no external deps)
  - `make test-integration` — integration + e2e tests; **needs Postgres**
    (CI spins up `pgvector/pgvector:pg16`). Run locally against the same image,
    or state explicitly that it was deferred to CI and why.
- **Source-of-truth doc:** `SPEC.md` at repo root (there is no `docs/` dir and
  no `check-doc-drift.sh`). The SPEC-drift check is a manual read, not a script.
- **Commit style:** `<area>: <summary>` (e.g. `deploy: ...`, `migrate: ...`,
  `smoke: ...`) or `fix(<area>): ...`; review fixes as
  `Address review pass N: <summary>`. PR titles end with `(#N)`.
</Repo_Facts>

<Flags>
- `--yes`: Skip the confirmation checkpoint before pushing the branch and
  opening the PR (step 6). Without it, autoplan always pauses there — pushing
  and opening a PR are visible, hard-to-fully-undo actions.
- `--interactive`: Passed through to ralplan (step 2) for draft-plan review and
  explicit approval before execution, instead of ralplan's default automated
  Planner→Architect→Critic run.
</Flags>

<Steps>
1. **Load the issue**:
   ```
   gh issue view <N> --json number,title,body,labels,state,url
   ```
   - If it doesn't exist or `state != OPEN`, stop and report — do not proceed on
     a closed or missing issue.
   - Also check for an existing open PR that references it
     (`gh pr list --search "<N> in:body" --state open`); if one exists, stop and
     report rather than duplicating work.
   - Parse the issue body for the standard sections (Summary, Context/Design,
     Scope, Acceptance Criteria, Affected modules, Risk/Rollback). If the issue
     lacks these, proceed but note the gap — ralplan's plan will infer scope
     from whatever prose is there.

2. **Ralplan the issue**: invoke `Skill("ralplan")` with the issue title, body,
   and labels as the task description (add `--interactive` if that flag was
   passed to autoplan). Wait for Critic `APPROVE` before continuing.
   - If ralplan's consensus loop materially changes scope from the issue (new
     acceptance criteria, different affected modules), update the GitHub issue
     body (`gh issue edit <N> --body ...`) so the issue stays the source of
     truth — don't let the plan and the issue diverge silently.
   - Several issues already have a consensus plan captured in auto-memory
     (`issue-<N>-*-plan`); if one exists for this issue, feed it to ralplan as
     prior art instead of planning from scratch.

3. **Resolve the base branch** — this repo has a single trunk, so this is
   simple, but never guess silently:
   a. Scan the issue body for an explicit target-branch override; if present,
      verify it exists (`git ls-remote --heads origin <branch>`) and use it.
   b. Otherwise the base is `master` (confirm via
      `gh repo view --json defaultBranchRef`). There are no `integration/*`
      branches to disambiguate — if any have appeared, treat two-plus equally
      plausible matches as genuine ambiguity and use `AskUserQuestion`.

4. **Branch**: create `feat/<N>-<slug>` (or `fix/<N>-<slug>` if the issue is
   labeled `bug`) off the resolved base. Slug from the issue title, kebab-case,
   short.

5. **Size the execution** off the approved plan's affected-module set and
   acceptance-criteria count — a concrete check, not a judgment call:

   | Signal | Route |
   |---|---|
   | 1 module, ≤5 acceptance criteria, not `risk`-labeled | `ralph` — single-thread persistent loop is enough |
   | 1 module, >5 acceptance criteria, or `risk`-labeled | `autopilot` — needs the full expand/plan/QA/validation lifecycle even for one workstream |
   | ≥2 modules (parallelizable workstreams, e.g. `indexer`+`app/search`) | `team` — parallel coordinated agents |

   Invoke the chosen skill (`Skill("ralph")`, `Skill("autopilot")`, or
   `Skill("team")`) with the ralplan-approved plan as input — do not let the
   execution skill re-plan from scratch when a consensus plan already exists.
   - Whichever route runs, it must write tests alongside the change (this repo's
     PRs ship unit + integration + e2e coverage) and keep the gates in step 6
     green.

6. **Pre-PR checks** (all required before a PR exists):
   - **Gates** (fresh output, not "should pass"):
     - `make lint`
     - `make test`
     - `make test-integration` — run it against local Postgres
       (`pgvector/pgvector:pg16`, same as CI). If Postgres genuinely isn't
       available locally, say so explicitly and note the suite will be validated
       by CI's `integration` job — do not silently skip it.
   - **SPEC-drift:** read `SPEC.md`; if the change alters behavior the SPEC
     documents, update `SPEC.md` in the same branch before opening the PR, never
     after.
   - **Code-review loop (max 2 cycles):** a fresh `code-reviewer` agent (add
     `security-reviewer` in parallel if the issue is `risk`-labeled or touches
     auth / OAuth / secret-scope / grants / SQL compilation) reviews the diff
     against the plan's acceptance criteria. This must run in a **separate
     context** from whichever agent implemented the change — never a
     self-review. Fix findings, re-review, stop after 2 cycles and report
     remaining findings if still unclean.

7. **Checkpoint** (unless `--yes`): summarize branch name, base branch, commits,
   and gate results; confirm with the user before pushing and opening the PR.

8. **Open the PR**:
   ```
   gh pr create --base master --head <feat/N-slug> \
     --title "<area>: <concise change> (#<N>)" \
     --body "Closes #<N>

   <summary>

   ## Test plan
   <real make lint / make test / make test-integration output>

   SPEC-drift check: <updated SPEC.md | no SPEC-visible change>"
   ```
   Match the repo's PR body shape (see PR #27): What & why, Design, Testing with
   real suite counts, Acceptance mapping. `Closes #NNN` in the body.

9. **Post-PR review loop** — the part that must produce real GitHub artifacts,
   not just in-context findings:
   a. Spawn a fresh reviewer (`code-reviewer`, `+security-reviewer` if
      risk-tagged) against the PR diff (`gh pr diff <PR>`).
   b. Post findings as an actual PR review: `gh pr review <PR> --comment
      --body "..."` for the summary, or `gh api
      repos/{owner}/{repo}/pulls/{PR}/comments` for line-anchored inline
      comments when a finding maps to a specific file/line.
   c. Fix the findings, commit as `Address review pass <k>: <short summary>`
      (this repo's actual convention, e.g. commit `29b70be`) — new commits,
      never a force-push/amend of pushed history.
   d. Push, then reply to / resolve the addressed threads so the PR shows what
      changed and why.
   e. Repeat a-d until the reviewer leaves zero new findings, capped at 2 cycles
      (same bound as step 6). If still unclean after 2 cycles, stop and hand off
      to a human reviewer with a summary of what's outstanding — do not loop
      indefinitely on your own review.

10. **Report**: PR URL, base branch, execution route taken and why, gate results
    (including whether `test-integration` ran locally or was deferred to CI), and
    review-loop outcome.
</Steps>

<Tool_Usage>
- `Skill("ralplan")`, `Skill("ralph")`, `Skill("autopilot")`, `Skill("team")` —
  these are skills, invoke via the `Skill` tool, not `Task`.
- `Task(subagent_type="code-reviewer", ...)` /
  `Task(subagent_type="security-reviewer", ...)` for both the pre-PR (step 6)
  and post-PR (step 9) review passes — always a fresh subagent, never the
  implementing context reviewing itself.
- `gh` CLI for all GitHub state (issue read/edit, PR create, PR review/comments,
  thread resolution). Never fabricate issue or PR content — read it fresh each
  step.
- `AskUserQuestion` only for genuine ambiguity — not for decisions this skill
  resolves deterministically (base is `master`; sizing is a table lookup).
</Tool_Usage>

<Examples>
<Good>
`autoplan 13`
→ loads issue #13 (`sym:` symbol search; affected modules `app/search` +
`app/main.py` + `indexer/symbols.py`) → ralplan confirms scope (a consensus
plan already exists in auto-memory) → base branch `master` → sizes to `team`
(≥2 modules) → branch `feat/13-symbol-search` → `make lint` / `make test` /
`make test-integration` green (332 passed) → checkpoint → PR opened,
`Closes #13`, base `master` → review loop posts findings, addressed in an
`Address review pass 1: ...` commit, resolved.
</Good>
<Bad>
`autoplan` with no issue number.
Why bad: nothing to load — ask for the issue number instead of guessing.
</Bad>
<Bad>
Opening the PR after skipping `make test-integration` without saying so.
Why bad: CI runs the integration job on every PR; a silently-skipped suite reads
as "all gates green" when it isn't. Either run it locally against the pg16 image
or state explicitly it's deferred to CI.
</Bad>
</Examples>

<Escalation_And_Stop_Conditions>
- Issue is missing, closed, or already has an open PR referencing it — stop and
  report rather than duplicating work.
- Any mandatory gate (`make lint` / `make test` / `make test-integration`) fails
  and can't be fixed in the current execution route — stop and report, do not
  open a PR with red gates.
- Post-PR review loop still has unresolved findings after 2 cycles — stop, hand
  off to a human reviewer with a clear summary of what's left.
- User says "stop", "cancel", or "abort" at any point.
</Escalation_And_Stop_Conditions>

<Final_Checklist>
- [ ] Issue loaded fresh, open, and not already covered by an open PR
- [ ] Ralplan consensus reached (Critic `APPROVE`)
- [ ] Base branch resolved (`master`, or a stated issue-body override)
- [ ] Branch named `feat/<N>-<slug>` / `fix/<N>-<slug>`
- [ ] Execution route chosen per the size table, with the signal that drove it stated
- [ ] `make lint` green (fresh output)
- [ ] `make test` green (fresh output)
- [ ] `make test-integration` green locally, or explicitly deferred to CI with reason
- [ ] SPEC.md checked (updated, or confirmed no SPEC-visible change)
- [ ] Pre-PR review loop clean (or reported unclean after 2 cycles)
- [ ] User checkpoint passed before push (unless `--yes`)
- [ ] PR opened with `Closes #NNN` and title ending `(#N)`
- [ ] Post-PR review loop: real PR comments posted, real fix commits pushed, threads resolved
- [ ] Final report includes PR URL and every routing decision's rationale
</Final_Checklist>
</content>
</invoke>
