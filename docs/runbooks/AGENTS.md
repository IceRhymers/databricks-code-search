<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# docs/runbooks

## Purpose

Operational runbooks for deploying and running the code-search system: the detail that is
too deep for the README but that an operator (possibly at 2am) actually needs. Each runbook
covers one feature area — what changed in storage, which deploys are grant-coupled, which
failures are expected versus alarming, and the exact recovery commands. The README links
into these files at specific heading anchors, so they are load-bearing documentation, not
scratch notes.

## Key Files

| File | Description |
|------|-------------|
| `multi-branch.md` | Multi-branch indexing (migration `0003`): `branches:` config globs and the 20-branch cap, grant-coupled deploy, exact (not glob) `branch:` query semantics, pgcrypto backfill, semantic branch-scoping EXPLAIN gate, and clean-run corpus reconciliation (#59): the fail-closed gate, the >50% purge shrink guard as an incident signal, and honest partial-vs-stale failure reporting |
| `semantic-enablement.md` | Semantic search default-on: the managed `shared_preload_libraries` project assumption (irreversible, out-of-band), enablement-is-just-deploy via migration `0004`, the three-surface opt-out flag, AI Gateway embeddings, memory/lock-window sizing, and rollback notes |
| `ci-lakebase.md` | The integration CI gate: why CI runs against a real ephemeral Lakebase branch (no Postgres stand-in can prove BM25 ranking or the ordered-index plan), operator prerequisites, per-run branch isolation/TTL safety properties, and the local manual-run recipe |
| `webui.md` | The `webui` Databricks App: deploy/URL/auth (`CAN_USE`, no OAuth app connection), branch permalinks and filter chips, the semantic tab and its status route, read-only grants, security headers, committed frontend build, and the deploy-time `app.whl` packaging seam |
| `indexing-parallelism.md` | Parallel indexing operations: killed runs are safe (per-repo transactional stamps), log reading, the three limits and disk math for `index_concurrency`, forcing a re-index (and who has permission to), the `INDEX_SEMANTICS_VERSION` bump obligation, and grant coupling |

## Subdirectories

None.

## For AI Agents

### Working In This Directory

- **Keep README links and anchors valid.** The README links specific anchors — e.g.
  `multi-branch.md#4-branch-query-semantics--exact-not-a-glob` — so renaming a heading (or
  a file) breaks those links silently. Grep `README.md` for `docs/runbooks/` before
  renaming anything; other runbooks also cross-link each other by filename and section.
- Runbooks reference code paths (`app/db/grants.py`, `indexer/job.py`, test names) as
  ground truth. If the code moves, update the runbook; if you edit a runbook, verify the
  referenced paths and test names still exist.
- Several runbooks record verified-live ground truth with dates (e.g. preload contents
  observed 2026-07-19) and explicitly-unverified items (e.g. the live `lakebase_ann`
  EXPLAIN capture). Preserve that verified/unverified distinction — do not promote an
  assumption to a fact.

### Testing Requirements

Nothing beyond link/anchor integrity — there is no test suite for these documents. After
edits, confirm intra-repo links resolve and README anchors still match the headings.

### Common Patterns

Conventions the existing runbooks share; follow them in new ones:

- Title states the scope, often with the issue or migration number
  (`# Runbook: multi-branch indexing (0003)`), followed by a short framing paragraph
  saying what an operator needs from the document.
- Numbered `##` sections, each a self-contained operational concern; anchors are stable
  once published.
- Grant-coupled changes get an explicit "Deploy coupling — this is NOT schema-only"
  section with the exact `make migrate ... ARGS=--apply-grants` command inline.
- Expected-versus-bug framing is spelled out ("treat that as the EXPECTED outcome, not a
  bug to chase"; "if the `UPDATE` fails on permissions, that is the expected failure").
- Known limitations are documented plainly rather than omitted, with the tracked follow-up
  named.
- Cross-references to sibling runbooks and `.omc/plans/` design docs go in a closing
  `## Reference` section or inline links, never duplicated prose.

## Dependencies

None — plain Markdown, no build step.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
