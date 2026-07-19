# Semantic search enablement (issue #14)

Operator runbook for turning on `semantic_search` (vector + BM25 hybrid RRF) on a
Lakebase project. This is a **deliberate, manual, per-project** operation --
never part of `make deploy` / `scripts/deploy.sh` -- because step 1 below is
irreversible and because the schema it creates depends on a beta extension pair
that must be approved for the target project first.

## 1. The real irreversible step: managed `shared_preload_libraries`

`lakebase_vector` and `lakebase_text` (and the `lakebase_tokenizer` extension
`lakebase_text` builds on) only load when the Databricks-**managed**
`shared_preload_libraries` for the Lakebase project already includes them. This
is **not** settable through the endpoint/project API or the bundle -- attempting
to set it that way returns `"setting cannot be changed"`. Getting it added is an
out-of-band request (UI / Databricks support) against the specific Lakebase
project, and **it is irreversible and project-level**: once added, it cannot be
removed from that project.

Until the preload includes them, `CREATE EXTENSION lakebase_vector` (and
`lakebase_text`) fails with an error to the effect of "must be loaded via
shared_preload_libraries". That failure is the **intended fail-loud signal** --
it means step 1 has not happened yet for this project, not that anything is
broken. Do not attempt to work around it; go get the preload added.

Ground truth (verified live against the `code-search` Lakebase project,
2026-07-19): before enablement the managed preload was
`neon,pg_stat_statements,databricks_auth,auto_explain`. After the Databricks-side
change added `lakebase_vector,lakebase_text`, `CREATE EXTENSION IF NOT EXISTS
lakebase_tokenizer` / `lakebase_vector` / `lakebase_text` all succeeded in that
order, and the gated revision's DDL (`vector_cosine_ops` ANN index via
`lakebase_ann`, `tsvector_bm25_ops` BM25 index via `lakebase_bm25`) built clean.
See `app/alembic/versions_semantic/0002sem_semantic_chunks.py` for the exact DDL
and access-method/opclass ground truth this depends on.

**Do this first, once per project, before anything below.** There is no
migration or script that performs it -- it is a support/UI action against the
Lakebase project itself.

## 2. Enablement order

Run these in order, against one target (`dev` or `prod`):

**(a) Managed preload (irreversible, out-of-band).** Confirm with Databricks
support / the workspace UI that `lakebase_vector` and `lakebase_text` are in the
project's managed `shared_preload_libraries`. Do not proceed until this is
confirmed -- the next step fails loudly if it isn't.

**(b) Gated migration.**

```
make migrate-semantic TARGET=<dev|prod>
```

This runs `scripts/migrate.py --semantic`, which:
- pre-checks that the core migration has already run (`files` table exists;
  fails loudly with a clear message otherwise -- run `make migrate
  TARGET=<target>` first if you haven't),
- prompts an interactive typed confirmation (`Type "enable-semantic" to
  proceed:`) before touching anything,
- applies `app/alembic/versions_semantic/0002sem_semantic_chunks.py` into a
  **separate** version table, `alembic_version_semantic` -- never the core
  `alembic_version` -- so it can never break a later core `make migrate` on this
  or any other project (see the revision's docstring for why a shared version
  table would be catastrophic).

This creates `chunks` (with `embedding vector(1024)`, generated `ts tsvector`,
the `lakebase_ann`/`vector_cosine_ops` ANN index, and the
`lakebase_bm25`/`tsvector_bm25_ops` BM25 index) as owned by whatever
role/identity ran the migration (normally the developer's own identity on dev,
or the deploying identity on prod).

**(c) Re-apply grants.** See section 3 -- this is the step that's easy to get
wrong.

**(d) Only now, turn the flag on.** Set `CODE_SEARCH_SEMANTIC_ENABLED=1` for the
app **after** (b) and (c) succeed. Order matters: the flag and the schema are
independent, so enabling first is a reachable state, and `semantic_search` cannot
infer the schema from the extensions (the `pg_am` capability probe tells it whether
the beta extensions are loaded, not whether `chunks` exists). Enabling early is
handled gracefully rather than catastrophically -- the tool returns a structured
`semantic_schema_missing` payload rather than raising -- but it returns no results
until the migration has run, and if you skip (c) the failure appears later, at index
or query time, as a permission error.

## 3. Grants reconciliation (the subtle part)

`chunks` did not exist when grants were last applied (during `make deploy` /
`scripts/deploy.sh`, step 6), so the app SP has no `SELECT` on it and the job SP
has no `INSERT`/`UPDATE`/`DELETE` or sequence usage on `chunks_id_seq`. The
wildcard grants in `app/db/grants.py` (`GRANT SELECT ON ALL TABLES IN SCHEMA`,
`GRANT INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA`, `GRANT USAGE ON ALL
SEQUENCES IN SCHEMA`) only cover tables/sequences that exist **at the moment the
grant runs** -- they are not retroactive. You must re-run the grants step now
that `chunks` exists.

Run this **as the owner of `chunks`** (the identity that ran `migrate-semantic`
in step 2b) or as a superuser -- an unprivileged role cannot grant on an object
it doesn't own. This is the same `make migrate ... ARGS=--apply-grants` step
`scripts/deploy.sh` runs at its step 6 (`grant_attempt`), just re-run after the
semantic migration:

```
# dev (app grant only)
APP_SP_ROLE=<app-sp-client-id> \
  make migrate TARGET=dev ARGS=--apply-grants

# prod (BOTH roles -- mirrors scripts/deploy.sh step 6)
APP_SP_ROLE=$(databricks apps get <app_name> -o json | jq -er '.service_principal_client_id') \
JOB_WRITER_ROLE=$JOB_RUN_AS_SP \
  make migrate TARGET=prod ARGS=--apply-grants
```

Notes:
- This is the plain `make migrate` target (**not** `make migrate-semantic`) --
  `scripts/migrate.py` deliberately rejects `--semantic --apply-grants`
  together (grants and the gated DDL are separate, independently-run
  concerns). `make migrate` re-running `upgrade head` against an
  already-current core schema is a no-op; only the grants step does new work.
- On prod, derive `APP_SP_ROLE` **fresh** from `databricks apps get` (client
  IDs can rotate) and set `JOB_WRITER_ROLE` to the same job run-as SP
  (`JOB_RUN_AS_SP`) used for the original deploy -- pass **both**, exactly as
  `scripts/deploy.sh`'s `cmd_full` step 6 does.
- `build_job_grants` (`app/db/grants.py`) already emits `GRANT USAGE ON ALL
  SEQUENCES IN SCHEMA ... TO <job_role>`, so re-running it after `chunks`
  exists covers the new `bigserial` `chunks_id_seq` too -- no separate sequence
  grant is needed.
- **Warning:** a bare `make migrate ARGS=--apply-grants` with no
  `JOB_WRITER_ROLE` set (or a dev-style app-only grant applied against prod)
  leaves the indexer's job SP without `INSERT` on `chunks` and without `USAGE`
  on `chunks_id_seq`. The indexer will then fail at index time with a
  permission-denied error the first time it tries to write chunks -- not at
  migration time, so this is easy to miss if you only check the migration
  step's exit code. Always pass both `APP_SP_ROLE` and `JOB_WRITER_ROLE` on
  prod.
- Verify: the app SP can `SELECT` from `chunks` (e.g. via the app's `/ready`
  probe, which already runs a protected-table `SELECT` as the app SP) and a
  subsequent indexing run with `semantic_enabled=true` can insert into
  `chunks` without error.

### Lock-window note (A4)

Embeddings are computed and buffered **outside** the indexing transaction
(`indexer/job.py` precomputes `(chunks, embeddings)` before opening
`index_repo`'s `conn.begin()`), so the write itself holds no model-serving call
under a repo row lock -- only plain DML. Before enabling on a project with large
repos, still confirm the Lakebase endpoint's
`idle_in_transaction_session_timeout` comfortably exceeds the time to write one
repo's buffered chunks, and that `semantic_max_chunks_per_repo`
(`app/config.py`, default 8000) is large enough for your largest repo -- indexing
fails loudly rather than silently truncating if a repo exceeds the ceiling.

That default is deliberately conservative: the buffered vectors are Python float
lists costing ~32 B per element, so at `dim=1024` each chunk is ~32 KB and 8000
chunks is ~260 MB resident, held for the duration of the repo's write
transaction. Raising it scales memory linearly (50000 would be ~1.6 GB and would
OOM a typical job container *before* the loud ceiling check could fire, which
defeats the purpose of the ceiling). If a repo legitimately needs more, prefer
the temp-table staging path (follow-up) over raising this number.

## 4. Rollback note

`app/alembic/versions_semantic/0002sem_semantic_chunks.py`'s `downgrade()` drops
the BM25/ANN indexes and the `chunks` table, but **does not** drop the
`lakebase_tokenizer` / `lakebase_vector` / `lakebase_text` extensions -- they are
database-wide objects, and dropping/recreating them buys nothing since the
managed-preload enablement (section 1) that makes them loadable is itself
irreversible per project. Downgrading the migration only removes the schema
objects this feature owns; it does not "disable" the project.

To disable the feature going forward without a downgrade, set
`semantic_enabled=false` (`CODE_SEARCH_SEMANTIC_ENABLED`) -- `semantic_search`
short-circuits before any DB/embedder access and the indexer skips chunk
writes entirely.

## 5. Deploy interaction

The semantic migration is **deliberately kept out of `scripts/deploy.sh`**. The
core deploy pipeline (`make deploy`) never touches a beta extension or the
`chunks` table -- `make migrate` inside `deploy.sh` only ever runs the core
`0001` history against the core `alembic_version` table. Enabling semantic
search is always a separate, deliberate, gated operator action: section 1 (once
per project, out-of-band) followed by sections 2-3 (`make migrate-semantic`
then re-grant), run manually whenever an operator chooses to turn the feature on
for an already-deployed project.
