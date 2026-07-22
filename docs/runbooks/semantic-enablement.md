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

**Two config surfaces, split by process (issue #36).** The MCP app (`code_search`) and
the webui app (`webui`) read semantic config from their **environment** —
`CODE_SEARCH_SEMANTIC_ENABLED=0` disables the feature there, short-circuiting before
any DB/embedder access. The **indexer job** has no reachable env surface (nothing in
`resources/job.yml` sets `CODE_SEARCH_*`), so it reads its semantic config from
`config.yaml`'s `semantic:` block instead (see section 6). Disable the job's semantic
indexing with:

```yaml
semantic:
  enabled: false
```

which makes the job a true semantic no-op (no embedder built, no chunking, the
2-worker clamp not applied) — no bundle/env change and no redeploy of the job's
environment. Precedence for the job is `config.yaml > CODE_SEARCH_* env > default`, so
`semantic.enabled: false` wins even if the env says enabled.

**All three surfaces, not just one:** the flag must be off on the MCP app, the webui
app (both via env), **and** the indexer job (via `config.yaml`) — each has its own
config source. The webui SPA's Semantic tab is driven entirely by
`GET /api/semantic/status`, which reads the **webui app's own** `cfg.semantic_enabled`
(see `docs/runbooks/webui.md`). Disabling only the MCP app leaves the webui tab
visible (and vice versa) — easy to mistake for a bug.

## 4. Embeddings: AI Gateway

Embeddings go through the workspace AI Gateway MLflow embeddings route
(`POST /ai-gateway/mlflow/v1/embeddings`, default model `system.ai.gte-large-en`,
1024-dim), via the SDK's workspace-authenticated raw API client (`app/embed.py`).
No model-serving endpoint needs to be provisioned. Overrides differ by surface: the
MCP / webui apps use env vars (`CODE_SEARCH_SEMANTIC_EMBEDDING_ENDPOINT` route path,
`CODE_SEARCH_SEMANTIC_EMBEDDING_MODEL`); the **indexer job** uses `config.yaml`'s
`semantic:` block (`embedding_endpoint`, `embedding_model` — see section 6). Swapping
`embedding_model` to a model of a different dimension is caught at **runtime** by
`EmbeddingDimMismatchError` (`app/embed.py`), which checks each returned vector against
the un-overridden `semantic_embedding_dim` and refuses to write bad vectors — that is
the guard that fires for the config surface, since the config block cannot (and does
not) move `semantic_embedding_dim`. (The unit dim tripwire is a separate, narrower
check: it only pins the *default* model's dimension to the column type, not a
config-supplied override.)

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

**Per-repo override, without moving the global default:** `config.yaml`'s top-level
`semantic_max_chunks_per_repo` map (`indexer/repo_config.py`) lets one outsized repo
get its own cap — `indexer/resolve.py` carries the matched override onto that repo's
`RepoEntry`, and `indexer/job.py` uses it in place of `cfg.semantic_max_chunks_per_repo`
for that repo only (an active override is logged at INFO). It does not relax the
2-worker semantic clamp above, so a large override still multiplies whichever of the
(at most 2) concurrent workers happens to be indexing that repo — do the same ~32
KB/chunk math against the override value, not just the global default, before setting
one. To move the **global** cap for the whole job instead of one repo, set
`semantic.max_chunks_per_repo` (a single int, section 6) — the map still wins for a
repo it names.

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

## 6. Indexer job config: the `semantic:` block

The serverless indexing job has no reachable env surface — nothing in
`resources/job.yml` sets `CODE_SEARCH_*`, so `Settings`' semantic defaults are
effectively hard-coded for the job. `config.yaml` is therefore the job's config
surface for semantic behavior; `indexer/job.py` overlays this block onto the process
`Settings` immediately after loading the config, before it sizes the worker pool or
builds the embedder. **Precedence is `config.yaml > CODE_SEARCH_* env > default`**; the
env vars remain the MCP-server / webui surface (separate processes, separate
environments) and this block does not reach them.

```yaml
semantic:
  enabled: true                    # -> Settings.semantic_enabled
  max_chunks_per_repo: 8000        # -> Settings.semantic_max_chunks_per_repo (GLOBAL ceiling)
  embedding_endpoint: /ai-gateway/mlflow/v1/embeddings
  embedding_model: system.ai.gte-large-en
  embedding_batch_size: 64
  embedding_timeout_s: 20.0
```

Every field is optional; an omitted field falls through to the env value / default, and
an absent `semantic:` block is a pure no-op (unmodified configs behave exactly as
before). Two `Settings` knobs are deliberately **not** exposed here:
`semantic_embedding_dim` (pinned to `SEMANTIC_EMBEDDING_DIM`, a schema invariant — the
`chunks.embedding` column type and the `0004` DDL derive from it) and
`semantic_chunk_max_tokens` (not consumed by the chunker, which is bounded by the
`SEMANTIC_CHUNK_MAX_CHARS` constant — a key for it would silently do nothing). A bad
value fails the run at parse time with the existing `ConfigError` contract (exit 1, path
in the message). `max_chunks_per_repo` here is the **global** ceiling; the top-level
`semantic_max_chunks_per_repo` **map** (section 4) spot-overrides individual repos and
still wins over this global.
