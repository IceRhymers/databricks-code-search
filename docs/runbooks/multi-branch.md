# Runbook: multi-branch indexing (0003)

What an operator needs to configure, deploy, and reason about once a repo's config
declares more than its default branch: what actually changed in storage, how the branch
cap and truncation behave, why this migration is grant-coupled like the parallelism one
before it, and two access-path caveats worth knowing before you page anyone about a
"regression."

---

## 1. What changed

Before `0003`, `files` was keyed `(repo_id, path)` and held exactly one content version per
path — the default branch's. `0003` re-keys it `(repo_id, path, content_sha)`: a path now has
one row **per distinct content version**, each carrying a `branches text[]` membership array
(GIN-indexed, `ix_files_branches_gin`) naming every branch whose tree currently resolves to
that content. Two branches with identical file content share one row (`branches` grows by
union); two branches that diverge on a path get two rows.

A new `repo_branches` table is the per-`(repo, branch)` index registry — one row per branch a
repo's config resolves to, each with its own `last_indexed_commit` / `last_indexed_at` /
`index_semantics_version` and its own CAS stamp. It replaces `repos`' single-branch stamp as
the source of truth for skip-if-unchanged and stale-write detection. `repos.last_indexed_commit`
/ `last_indexed_at` / `index_semantics_version` are **deprecated but retained for one release**
(written by the default-branch run only, without CAS, so a 0002-era reader still gets something
sane) — do not add new readers or writers of those three columns; a later cleanup migration
drops them.

Queries are scoped to a repo's default branch **unless** the query names a branch explicitly:
`search_code`/`semantic_search`'s `branch` parameter, or a `branch:<name>` atom in the
`search_code` query string. See the [README's query-language section](../../README.md#query-language)
and [MCP tools table](../../README.md#mcp-tools) for the caller-facing syntax; this runbook
covers the indexing/deploy side.

## 2. Configuring which branches get indexed

`config.yaml`'s `branches:` key (per connection) is a list of glob patterns matched against
each repo's branch list, in addition to its default branch, which is **always** included
regardless of whether it matches:

```yaml
connections:
  - type: github
    orgs:
      - acme
    branches:
      - "main"
      - "release/*"
      - "feature/hot-*"
```

**Empty (the default, and every pre-0003 config unmodified) means default-branch-only** — no
behavior change for a config that doesn't set `branches:`, no reindex forced, and the GitHub
branches API is not even called in that case (`indexer/job.py` skips straight to the default
branch's SHA).

Matching is `fnmatchcase` (a plain glob, case-sensitive, identical on every platform) against
the exact branch name — not a regex, and not the `~*` semantics `repo:`/`file:` use at query
time. `branch:` at query time is likewise an exact match, not a glob (see §4).

**The soft cap is 20 branches per repo** (`indexer/branches.SOFT_BRANCH_CAP`). If globs resolve
to more than 20 branches for one repo, the result is truncated **default-first, then
alphabetical** — the default branch is never dropped, and among the rest it's alphabetical
order that decides what survives. Truncation is a loud `logger.warning` naming the repo, the
cap, and the exact dropped branches, not a failure: the run still indexes the kept 20. There is
no override flag — mirroring `indexer.resolve.MAX_REPOS`' philosophy, the fix for a runaway
match (a typo'd `*` glob, say) is narrowing the glob in `config.yaml`, not a config knob to raise
the ceiling.

**Since #61, truncation also marks that repo's branch discovery incomplete and blocks
reconciliation for it.** `indexer.branches.resolve_branches` returns a typed `BranchResolution`
(`branches`, `complete`, `dropped`, `cap`) instead of a bare list, and `indexer.job`'s per-repo
`RepoOutcome` carries the same `complete` flag through as `discovery_complete`. A capped repo's
kept branches are still indexed normally — nothing here changes what gets indexed or the run's
exit code — but the desired-state reconciliation checkpoint (#56/#59, see §7) treats a truncated
set as incomplete discovery and blocks reconciliation **corpus-wide** for that run, not just for
the capped repo: it must not treat a truncated set as proof a repo has no other branches, which
would let it retire branches only dropped by the cap, not genuinely removed upstream. Narrow the
glob (or raise the repo's branch hygiene) to get back to `complete=True` and unblock
reconciliation.

Branches are indexed **sequentially within a repo, not in parallel** (Option A1). This is
deliberate, not a missing optimization: it's what keeps the single-writer-per-repo invariant the
parallelism work established (see [`indexing-parallelism.md`](indexing-parallelism.md)) sound
under multi-branch — two branches of the same repo never race each other's array-union upsert or
membership sweep on `files`. A repo with many branches parallelizes *across* repos as before; a
20-branch repo is serial within its one worker slot. At the ~20 cap and the hundreds-of-repos
design target, this is the accepted trade.

## 3. Deploy coupling — this migration is NOT schema-only

Same shape as the parallelism migration before it: `repo_branches` is a new table, and a bare
schema-only migrate does not grant the app/job roles anything on it. **Always deploy this with
`scripts/deploy.sh full` (i.e. `make deploy`) or, for an already-deployed target, re-run the
grants step explicitly:**

```
APP_SP_ROLE=<app-sp-client-id> JOB_WRITER_ROLE=<job-run-as-sp-client-id> \
  make migrate TARGET=<target> ARGS=--apply-grants
```

**Never run a bare schema-only migrate against an existing deployment** (`make migrate
TARGET=<target>` with no `ARGS=--apply-grants`) and stop there — `repo_branches` would exist
with no `SELECT`/`INSERT`/`UPDATE`/`DELETE` grant for either role, and the next indexing run or
`list_repos` call fails with `permission denied for table repo_branches`. `build_job_grants` /
`build_app_grants` (`app/db/grants.py`) use `GRANT ... ON ALL TABLES IN SCHEMA` +
`ALTER DEFAULT PRIVILEGES`, so the grants step covers the new table with no code change — it
just has to actually run. Grants and `upgrade head` are both idempotent, so re-running the
grant step on a target that's already current is safe.

## 4. `branch:` query semantics — exact, not a glob

Deliberately diverging from `repo:`'s regex (`~*`): `branch:<name>` lowers to the GIN-served
exact-membership operator `files.branches @> ARRAY['<name>']` (Option C1). There is no
query-time branch globbing or regex in V1 — `branch:feature/*` returns nothing, because globs
are a **config-time** concept (§2), not a query-time one. If query-time branch globbing is
needed later, the AST node can carry a mode flag as an additive follow-up; nobody has asked for
it yet, and a regex/`EXISTS(unnest(...))` lowering would defeat the GIN index for a capability
this ships without.

Without an explicit `branch:` atom (or the `branch` tool parameter), `search_code` /
`semantic_search` / `get_file` scope to each repo's default branch via a correlated
`coalesce(repos.default_branch, 'HEAD') = ANY(files.branches)` conjunct — the exact same
`coalesce(...,'HEAD')` expression at all four sites that need it: the `0003` backfill, the query
compiler's implicit conjunct, the semantic default leg, and `get_file`'s default resolution. A
`NULL` `repos.default_branch` (never actually written by `index_repo`, but reachable in
principle, e.g. a hand-inserted row) resolves to `'HEAD'` identically everywhere.

### Access-path note: the implicit default conjunct is not GIN-served

This is expected planner behavior, not a defect, and it's worth knowing before you go looking
for a regression. The implicit default-branch conjunct is a **correlated `EXISTS`** against
`repos` (`app/query/compiler.py::_default_branch_conjunct`) — it has to be correlated because
each file is checked against *its own repo's* default branch, not a constant, so it cannot be a
plain indexed predicate. It runs behind whatever trgm/content scan the rest of the query's
predicate reaches, and on a **small corpus** Postgres's cost model can legitimately prefer a
plain Index Scan of the (small) `uq_files_repo_path_sha` unique btree + a Filter over the
trigram Bitmap Heap Scan — even after `ANALYZE`. Both plans return identical, correct results;
only the *access path* differs, and only on tiny corpora (the same class of small-data planner
artifact already documented for the pre-`0003` regex-shape tests).

`tests/integration/test_query_compiler.py::test_explain_trigram_extractable_regex_uses_gin_index`
is `xfail(strict=False)` for exactly this reason post-`0003` — it can no longer isolate a WHERE
clause that leaves the trgm GIN index as the *only* index able to satisfy the query, because
every default query now also joins `repos` via that correlated `EXISTS`.
`test_branch_filter_query_uses_branches_gin_index` is the still-deterministic replacement
proof: an **explicit** `branch:` predicate opts out of the implicit conjunct entirely (see
`_has_branch_filter`), lowers to the GIN-served `@>` operator with nothing else in the WHERE
clause, and `ix_files_branches_gin` is the *only* index able to satisfy it — so that path stays
provably index-served independent of corpus size. If you need a deterministic GIN-path proof for
an EXPLAIN capture, use an explicit `branch:` query, not a bare default one on a small corpus.

## 5. Backfill: B1 chosen (in-DB `pgcrypto` digest)

The Phase-0 gate (`.omc/plans/open-questions.md`) resolved **B1**: `0003` backfills
`content_sha` in-DB via `encode(digest(coalesce(content,''),'sha256'),'hex')`, gated on
`CREATE EXTENSION IF NOT EXISTS pgcrypto` succeeding under the migrator identity
(`tests/integration/test_content_sha_parity.py::test_pgcrypto_creatable_by_migrator`) and on
byte-for-byte parity with `indexer/hashing.py::content_sha` across ASCII, multibyte UTF-8
(`"héllo→λ"`), empty string, `None`, and trailing-`\n` content
(`test_content_sha_matches_pgcrypto_digest`, 5/5 cases green). No `scripts/backfill_content_sha.py`
and no follow-up NOT-NULL-flip migration was needed — the B2 two-phase Python fallback stays
documented as the escape hatch only if a *future* target lacks `pgcrypto` privileges (unlikely,
but the migration would fail loudly at `CREATE EXTENSION` if so, not silently diverge).

`indexer/hashing.py::content_sha` is the single canonical hash helper — the indexer's per-file
upsert and this backfill must never diverge, or dedup silently breaks on the first post-migration
index of an unchanged repo (the pre-mortem this parity test exists to close off).

## 6. Semantic branch scoping: index-path status

Option D1 (join `chunks → files → repos` inside each RRF leg's inner subquery, filter before
`ORDER BY metric LIMIT :topk`) is what shipped — see `app/search/semantic.py::_leg_cte` /
`_branch_predicate`. Two different things are true about its index-path health, and they should
not be conflated:

- **Proven on the `standin` backend (pgvector, this repo's CI/local Postgres).**
  `tests/integration/test_semantic_rrf.py::test_ann_leg_uses_hnsw_index_not_seqscan_sort` EXPLAINs
  the joined+filtered query and confirms the ANN leg still rides `ix_chunks_embedding_hnsw` (an
  `Index Scan`, not a `Seq Scan` + `Sort`) — the join does not defeat the HNSW index on this
  backend.
- **NOT yet proven on the live `lakebase_ann` backend.** The Phase-0 open item
  (`.omc/plans/open-questions.md`, "Semantic ANN index path under the branch-filter join") is
  still unchecked: it explicitly requires an `EXPLAIN (ANALYZE, BUFFERS)` capture against a real,
  semantic-enabled Lakebase project, which this development environment does not have access to.
  `pgvector`'s HNSW and Lakebase's `lakebase_ann` are different index implementations behind the
  same `<=>` operator syntax, so the standin result is evidence, not proof, for production.

**Before enabling semantic search on a target where branch-scoped queries matter** (i.e. any
target with `branches:` configured for at least one repo), capture the live `EXPLAIN` as part of
the [semantic-enablement runbook](semantic-enablement.md)'s rollout: run a `branch=`-scoped
`semantic_search` call (or the raw SQL from `build_hybrid_rrf_sql("lakebase", branch=...)`)
through `EXPLAIN (ANALYZE, BUFFERS)` and confirm the ANN leg still reaches `lakebase_ann`'s index
rather than falling back to a sequential scan + sort.

**If the live capture shows the filtered ANN loses its index path — treat that as the EXPECTED
outcome, not a bug to chase.** Filtered vector search is known-hard in general; the plan's D1
risk assessment always expected this to be the likely shipping shape on the real backend. The
documented fallback is to raise `SEMANTIC_TOP_K` (over-fetch each leg's un-filtered candidate
set) and post-filter for default/`branch:` membership *after* fusion — a recall cost, not a
correctness one, and it only needs a config-level change (no schema migration) if you hit it.
Record whichever outcome you observe in this section once verified against a real project.

## 7. Clean-run corpus reconciliation

When a branch stops being resolved — a default-branch flip (`master` → `main`), a branch
deleted on GitHub, or a `config.yaml` glob narrowed to no longer match it — or when a whole repo
drops out of the resolved config, `indexer/job.py::run()` reconciles it away automatically. This
closes the gap the pre-#59 limitation used to describe ("retired-branch membership is never
swept"): the per-branch membership sweep (`indexer/store.py::index_repo`) only ever touches
branches it is *currently* indexing, so it alone can never know a branch (or a whole repo) was
dropped entirely. A dedicated post-fan-out checkpoint now does.

**The clean-run gate is fail-closed, and it is corpus-wide.** After every worker in a run has
joined (`ThreadPoolExecutor`'s `with` block exits), `indexer.job._decide_reconciliation` checks
the WHOLE run, not just the repo in question:

- zero branch/repo `failed` outcomes,
- zero branch `conflict` outcomes (conflicts self-heal on the next run — see §1 — but are not
  themselves evidence the corpus can be trusted this run),
- every resolved repo produced a result (no repo silently dropped out of the count), and
- every resolved repo's branch discovery was **complete** (`discovery_complete` / `BranchResolution.complete`, see §2) — a single soft-cap-truncated repo blocks reconciliation for *every* repo in the run, not just itself.

Any one of those failing means the run indexed successfully (or partially) but reconciliation is
skipped entirely — the corpus stays exactly as it was, one run stale rather than wrongly pruned.
This is the deliberate **stale-over-destructive** philosophy: an operator can always re-run to
retry reconciliation, but a wrongly deleted row is not similarly recoverable. Only when the gate
passes does the checkpoint open one post-fan-out connection and, per resolved repo, retire any
branch present in its persisted `repo_branches` registry but absent from this run's resolved set,
then purge any stored repo absent from this run's fully resolved repo set.

**A large corpus shrink is withheld, not applied, as an incident signal.** If a clean run's purge
would remove more than half of the currently-stored repos in one pass
(`indexer.job.MAX_PURGE_SHRINK_FRACTION = 0.5`, no config override), the repo-level purge alone is
withheld — retired-*branch* cleanup on the surviving repos still applies normally, since that part
is provably safe on repos this run genuinely, completely resolved. The run logs an ERROR and exits
non-zero. This exists because a narrowed GitHub token/org/repo scope returns a clean HTTP 200 with
fewer repos than before — indistinguishable, by count alone, from a legitimate mass decommission —
and would otherwise silently mass-purge a live corpus. **Before assuming this is a legitimate
config change: check the indexing job's GitHub token still has read access to every org/repo the
resolved config expects** (a 403 on enumeration fails the run outright and would never reach this
guard, but a *narrowed org membership* can still enumerate successfully with fewer repos). Once
you've confirmed the shrink is real config intent — not credential narrowing — there are two ways
to complete it:

- **Staged removal (the normal path):** remove at most half of the currently-stored repos from
  `config.yaml` per clean run. Each run's purge then falls at or under the 50% guard and the
  corpus converges to the new desired state over a few runs.
- **Manual one-time purge (for repos that are already absent from config but were never applied
  because of this guard):** once token scope is confirmed, run
  `indexer.store.reconcile_removed_repos(conn, desired_repos=<the resolved repo names>)` directly
  against the database (the same primitive the job calls) to apply the withheld purge in one
  shot, bypassing the per-run guard for this one operator-confirmed action.

A withheld purge is always visible in the job log as an ERROR naming the would-purge count against
the stored count — that line is the incident signal to alert on, not a routine "nothing to do"
skip.

**Failure mid-reconciliation is reported honestly, never as silent success.** Each store primitive
(`reconcile_retired_branches`, `reconcile_removed_repos`) keeps its own transaction, so a failure
partway through leaves whatever already committed in place. The run's log line distinguishes
"corpus PARTIALLY reconciled" (something committed before the error — the rest completes on the
next clean run, since both primitives are idempotent) from "corpus left stale" (nothing committed
at all) — and always exits non-zero either way. The logged error never includes the raw exception
message or a traceback, only the phase, the repo (where applicable), and the exception's type
name, matching this job's existing token-redaction discipline.

`indexer.branches.BranchResolution.complete` (#61, see §2) is the property the gate reads directly
— a repo whose discovery was truncated (`complete=False`) blocks reconciliation for the whole run
until the config is narrowed back to a complete resolution.

---

## Reference

- [`.omc/plans/multi-branch-support-plan.md`](../../.omc/plans/multi-branch-support-plan.md) —
  the full consensus-approved design (schema, indexer, query layer, serve).
- [`.omc/plans/open-questions.md`](../../.omc/plans/open-questions.md) — the Phase-0 gate
  result (B1 decision); the retired-branch-sweep follow-up it tracked is resolved by §7 (#59).
- [`indexing-parallelism.md`](indexing-parallelism.md) — the single-writer-per-repo invariant
  this migration extends to per-branch sequencing.
- [`semantic-enablement.md`](semantic-enablement.md) — the semantic search rollout this
  runbook's §6 EXPLAIN gate is a step inside.
