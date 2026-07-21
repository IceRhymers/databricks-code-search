# Semantic search (issue #14) — default-on

`semantic_search` (vector + BM25 hybrid RRF) is **enabled by default**: no env var is
needed on the MCP app, the webui app, or the indexer job. Enablement for a target is
simply `make deploy TARGET=<dev|prod>` — the `chunks` DDL rides the core migration
chain (`app/alembic/versions/0004_semantic_chunks.py`, applied by `make migrate` at
deploy step 5), and the deploy's own grant steps (7 and 9) cover it.

## 1. Project assumption: managed `shared_preload_libraries`

**Stated project assumption:** every target Lakebase project (dev, prod, and the
`code-search-ci` CI project) has `lakebase_vector,lakebase_text` in its
Databricks-**managed** `shared_preload_libraries`. This was formerly a gate; it is now
an assumption the migration relies on.

The preload is **not** settable through the endpoint/project API or the bundle —
attempting to set it that way returns `"setting cannot be changed"`. Getting it added
is an out-of-band request (UI / Databricks support) against the specific Lakebase
project, and **it is irreversible and project-level**: once added, it cannot be
removed from that project.

If a project lacks the preload, `CREATE EXTENSION lakebase_vector` (and
`lakebase_text`) fails with "must be loaded via shared_preload_libraries" — now
surfacing at `make migrate` / `make deploy` time. That failure is the **intended
fail-loud signal**: it means the assumption does not hold for this project yet. Do not
work around it; go get the preload added, then re-run the deploy.

Ground truth (verified live against the `code-search` Lakebase project, 2026-07-19):
before enablement the managed preload was
`neon,pg_stat_statements,databricks_auth,auto_explain`. After the Databricks-side
change added `lakebase_vector,lakebase_text`, `CREATE EXTENSION IF NOT EXISTS
lakebase_tokenizer` / `lakebase_vector` / `lakebase_text` all succeeded in that order,
and the revision's DDL (`vector_cosine_ops` ANN index via `lakebase_ann`,
`tsvector_bm25_ops` BM25 index via `lakebase_bm25`) built clean. See
`app/alembic/versions/0004_semantic_chunks.py` for the exact DDL and
access-method/opclass ground truth this depends on.

Additional ground truth (2026-07-21, a second live project): `lakebase_vector`
declares a dependency on the base `vector` extension, so on a project where `vector`
is not pre-installed a bare `CREATE EXTENSION lakebase_vector` fails with
`required extension "vector" is not installed`. The migration therefore uses
`CREATE EXTENSION ... CASCADE`, which installs declared dependencies — and does
**not** mask the preload fail-loud signal above.

## 2. Enablement = deploy

```
make deploy TARGET=<dev|prod>
```

That is the whole procedure. Specifically, the pipeline (`scripts/deploy.sh`):

- **step 5 (`make migrate`)** applies the core chain up to `0004`, which creates the
  extensions, `chunks` (including the `start_line`/`end_line` columns, issue #44), and
  both lakebase indexes. On a project that already ran the retired
  `make migrate-semantic`, `0004`'s `to_regclass('chunks')` guard skips the CREATE,
  adds the line-range columns, and drops the orphaned `alembic_version_semantic`.
- **steps 7 and 9** re-apply the wildcard grants **after** the migration, so both app
  SPs get `SELECT` on `chunks` and (prod) the job SP gets write + `chunks_id_seq`
  usage. No manual grant reconciliation is needed on the deploy path.

**Existing deployments:** the next `make deploy` on a target that predates `0004` runs
the migration and then the idempotent grants — no manual reconciliation. The
`INDEX_SEMANTICS_VERSION` bump (`app/db/models.py`) forces every already-indexed
branch to re-index once on the next job run, which backfills `chunks` (and its line
ranges); semantic results appear after that run completes.

## 3. Opt-out

To disable the feature, set `CODE_SEARCH_SEMANTIC_ENABLED=0` explicitly —
`semantic_search` short-circuits before any DB/embedder access and the indexer skips
chunk writes entirely.

**All three surfaces, not just one (issue #36):** the flag must be set on the MCP app
(`code_search`), the webui app (`webui`), **and** the indexer job — each has its own
environment. The webui SPA's Semantic tab is driven entirely by
`GET /api/semantic/status`, which reads the **webui app's own** `cfg.semantic_enabled`
(see `docs/runbooks/webui.md`). Disabling only the MCP app leaves the webui tab
visible (and vice versa) — easy to mistake for a bug.

## 4. Embeddings: AI Gateway

Embeddings go through the workspace AI Gateway MLflow embeddings route
(`POST /ai-gateway/mlflow/v1/embeddings`, default model `system.ai.gte-large-en`,
1024-dim), via the SDK's workspace-authenticated raw API client (`indexer/embed.py`).
No model-serving endpoint needs to be provisioned. Overrides:
`CODE_SEARCH_SEMANTIC_EMBEDDING_ENDPOINT` (route path) and
`CODE_SEARCH_SEMANTIC_EMBEDDING_MODEL` — a model with a different dimension trips the
dim tripwire test / `EmbeddingDimMismatchError` rather than writing bad vectors.

On a workspace where the route or model is unavailable, the indexer degrades
gracefully (indexes the core corpus without chunks, logging loudly —
`indexer/job.py`); an MCP/webui query fails as a logged tool error.

### Lock-window / memory note (A4)

Embeddings are computed and buffered **outside** the indexing transaction
(`indexer/job.py` precomputes `(chunks, embeddings)` before opening `index_repo`'s
`conn.begin()`), so the write itself holds no network call under a repo row lock —
only plain DML. On a project with large repos, confirm the Lakebase endpoint's
`idle_in_transaction_session_timeout` comfortably exceeds the time to write one repo's
buffered chunks, and that `semantic_max_chunks_per_repo` (`app/config.py`, default
8000) is large enough for your largest repo — indexing fails loudly rather than
silently truncating if a repo exceeds the ceiling.

That default is deliberately conservative: the buffered vectors are Python float lists
costing ~32 B per element, so at `dim=1024` each chunk is ~32 KB and 8000 chunks is
~260 MB resident, held for the duration of the repo's write transaction. Raising it
scales memory linearly (50000 would be ~1.6 GB and would OOM a typical job container
*before* the loud ceiling check could fire, which defeats the purpose of the ceiling).
If a repo legitimately needs more, prefer the temp-table staging path (follow-up) over
raising this number.

**Parallelism:** with semantic on by default, the `effective_workers` clamp to 2 in
`indexer/job.py` now applies to every index run by default (each worker materialises a
whole repo's chunks) — see `docs/runbooks/indexing-parallelism.md`.

## 5. Rollback note

`0004`'s `downgrade()` drops the BM25/ANN indexes and the `chunks` table, but **does
not** drop the `lakebase_tokenizer` / `lakebase_vector` / `lakebase_text` extensions —
they are database-wide objects, and dropping/recreating them buys nothing since the
managed-preload change (section 1) is itself irreversible per project. Downgrading
only removes the schema objects this feature owns; it does not "un-preload" the
project. For a behavioral rollback, prefer the opt-out flag (section 3).
