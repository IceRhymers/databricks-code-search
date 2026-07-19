# Runbook: running CI against a real Lakebase engine

`.github/workflows/ci-lakebase.yml` runs the integration suite against a real
Lakebase branch instead of a `pgvector` container. **It is disabled** until the
prerequisites below are met.

## Why

The semantic layer's production query path uses two things that exist in no
Postgres image:

- `lakebase_ann`'s `<=>` served by an **ordered-index scan**, and
- `lakebase_bm25`'s `ts <@> to_bm25query(...)` **BM25 scorer**.

`ci.yml`'s container job substitutes pgvector `<=>` plus `ts_rank_cd`. That
proves the RRF fusion plumbing and the ANN leg, but it can prove neither BM25
**ranking** nor the BM25 leg's **query plan** — its stand-in metric is never
index-ordered, so a plan collapse there is invisible. A regression of exactly
that kind (an inner `ORDER BY` tiebreak that made the ordered-index path
unavailable) was caught in code review rather than by CI, and would have been a
production-only failure. This workflow closes that gap.

## Prerequisites

1. **Provision the CI project.** A Lakebase Autoscaling project separate from
   dev/prod — default id `code-search-ci`, declared as the `ci` bundle target in
   `databricks.yml`. It must be separate: every run creates and drops extensions
   and tables, which must never happen in a data-bearing project.

2. **Enable the beta preload on it.** Its Databricks-managed
   `shared_preload_libraries` must include `lakebase_vector,lakebase_text`. This
   is the same irreversible, out-of-band step described in
   [`semantic-enablement.md`](semantic-enablement.md) §1 — it is not settable via
   the API. Without it, the workflow's semantic migration step fails with
   `must be loaded via shared_preload_libraries`.

3. **Keep the CI project's `production` branch UN-MIGRATED.** This is the
   non-obvious one. Every run forks `production`, and forks inherit *installed*
   extensions. `test_no_vector_extension_installed` asserts that the core
   migration leaves no vector-family extension installed, so it fails against a
   fork of an already-migrated parent. The parent stays clean; each run's branch
   installs what it needs and is then purged.

4. **Configure repository auth and settings.**
   - Secrets: `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`
     for a service principal with permission to create/delete branches on that
     project. Prefer OIDC federation over a long-lived client secret once the
     workspace supports it.
   - Variables: `CI_LAKEBASE_ENABLED=true` (the guard), and optionally
     `CI_LAKEBASE_PROJECT` to override the project id.

5. **Add the triggers.** The workflow deliberately has no `pull_request` / `push`
   trigger — see the commented block under `on:`. Add them when enabling.

## How a run works

Each run forks a throwaway branch (copy-on-write, so it is cheap), applies the
core migration, applies the **gated semantic migration** against the real beta
extensions, runs `make test-integration` with `CODE_SEARCH_SEMANTIC_ENABLED=1`,
and purges the branch in an `always()` step.

Two safety properties worth preserving if you edit it:

- **Per-run branch isolation.** The suite drops extensions and recreates tables;
  a shared database would let concurrent PRs corrupt each other.
- **Branch TTL.** `scripts/ci_branch.py` sets a 2h TTL, so a cancelled run that
  never reaches teardown cannot leak a branch indefinitely. Teardown is also
  best-effort by design — a purge failure logs a warning rather than masking a
  real test failure.

## Cost

Branches are copy-on-write and endpoints scale to zero, so the steady-state cost
is roughly the CI project's storage plus the compute of each run. The TTL bounds
the worst case (a leaked branch) to hours rather than indefinitely.

## After it is green

`ci.yml`'s `integration` job becomes redundant: this job covers everything it
covers, against the real engine. Remove the container job at that point rather
than paying for both. Until then, keep both — the container job is currently the
only integration coverage that actually runs.
