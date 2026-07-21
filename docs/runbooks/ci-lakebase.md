# Runbook: CI against a real Lakebase engine

`.github/workflows/ci-lakebase.yml` runs the integration suite against an ephemeral
Lakebase branch. It is **the project's only integration gate** — this project is
Lakebase-only, and no Postgres image can stand in for the production operators. The
workflow triggers on every PR and on pushes to `master`, but the
`vars.CI_LAKEBASE_ENABLED` guard keeps the job inert (skipped) until the
prerequisites below are met.

## Why Lakebase-only

The semantic layer's production query path uses two things that exist in no Postgres
image:

- `lakebase_ann`'s `<=>` served by an **ordered-index scan**, and
- `lakebase_bm25`'s `ts <@> to_bm25query(...)` **BM25 scorer**.

A pgvector stand-in can prove RRF fusion plumbing, but neither BM25 **ranking** nor
the BM25 leg's **query plan** — a stand-in metric is never index-ordered, so a plan
collapse there is invisible. A regression of exactly that kind (an inner `ORDER BY`
tiebreak that made the ordered-index path unavailable) was caught in code review
rather than by CI, and would have been a production-only failure. Running on a real
branch closes that gap; the former pgvector container job in `ci.yml` is gone.

**Until the prerequisites are met, integration coverage is zero** (the guard skips
the job on every PR). Provision the CI project promptly after landing this workflow.

## Prerequisites (operator, out-of-band)

1. **Provision the CI project.** A Lakebase Autoscaling project separate from
   dev/prod — default id `code-search-ci`, declared as the `ci` bundle target in
   `databricks.yml`. It must be separate: every run creates and drops tables, which
   must never happen in a data-bearing project.

2. **Enable the preload on it.** Its Databricks-managed `shared_preload_libraries`
   must include `lakebase_vector,lakebase_text` — the stated project assumption (see
   [`semantic-enablement.md`](semantic-enablement.md) §1; irreversible, not settable
   via the API). Without it, the workflow's migrate step fails with
   `must be loaded via shared_preload_libraries`.

3. **Configure repository auth and settings.**
   - Secrets: `DATABRICKS_HOST`, `DATABRICKS_CLIENT_ID`, `DATABRICKS_CLIENT_SECRET`
     for a service principal with permission to create/delete branches on that
     project. Prefer OIDC federation over a long-lived client secret once the
     workspace supports it.
   - Variables: `CI_LAKEBASE_ENABLED=true` (the guard), and optionally
     `CI_LAKEBASE_PROJECT` to override the project id.

## How a run works

Each run forks a throwaway branch off `production` (copy-on-write, so it is cheap),
runs `scripts/migrate.py` (the core chain includes the `0004` semantic `chunks`
revision — no separate semantic step, no ack), runs `make test-integration`
(semantic is default-on; integration tests use fake embedders, so no embedding-
endpoint cost), and purges the branch in an `always()` step. A pre-migrated parent
branch is fine: `upgrade head` is idempotent, and every test isolates its DDL in a
throwaway schema.

Two safety properties worth preserving if you edit it:

- **Per-run branch isolation.** The suite creates and drops schemas/tables; a shared
  database would let concurrent PRs corrupt each other.
- **Branch TTL.** `scripts/ci_branch.py` sets a 2h TTL, so a cancelled run that never
  reaches teardown cannot leak a branch indefinitely. Teardown is also best-effort by
  design — a purge failure logs a warning rather than masking a real test failure.

## Local / manual runs

The same pattern works for a manual integration pass or `make migration`
autogenerate:

```
uv run python scripts/ci_branch.py up --project code-search-ci --branch <slug>
# exports LAKEBASE_ENDPOINT / LAKEBASE_DATABASE
uv run python scripts/migrate.py
make test-integration
uv run python scripts/ci_branch.py down --project code-search-ci --branch <slug>
```

## Cost

Branches are copy-on-write and endpoints scale to zero, so the steady-state cost is
roughly the CI project's storage plus the compute of each run. The TTL bounds the
worst case (a leaked branch) to hours rather than indefinitely.
