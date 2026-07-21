<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# app/alembic

## Purpose
The Alembic migration environment and the single linear core revision chain (0001 → 0004) for the code-search schema on Lakebase Postgres. `env.py` resolves its connection in strict priority order: an injected connection from `scripts/migrate.py` (which owns the Lakebase OAuth engine) wins; else `LAKEBASE_ENDPOINT`/`PGHOST` builds one via `create_db_engine()` (the `make migration` autogenerate path against a disposable Lakebase branch); else it raises — there is no implicit default. Autogenerate diffs against `app.db.models.Base.metadata`, with the semantic `chunks` surface filtered out entirely. `alembic.ini` at the repo root points `script_location` here.

## Key Files
| File | Description |
|------|-------------|
| `env.py` | Connection resolution (injected > env-driven > raise), offline `--sql` mode via `DATABASE_URL`, optional `version_table_schema` attribute, and the `include_object` filter that makes `chunks` (and its indexes) invisible to autogenerate — without it autogenerate would emit `drop_table('chunks')`. Never imports the Databricks SDK |
| `script.py.mako` | Revision template for `make migration MSG="..."` |
| `versions/` | The core revision chain (below). No separate AGENTS.md — this file covers it |

### versions/
| File | Description |
|------|-------------|
| `0001_initial_core_schema.py` | `CREATE EXTENSION pg_trgm` first (so `gin_trgm_ops` resolves), then `repos`/`files`/`symbols` plus the three trgm GIN indexes (`ix_files_content_trgm`, `ix_files_path_trgm`, `ix_symbols_name_trgm`). Downgrade drops tables in reverse dependency order but never the extension |
| `0002_index_semantics_version.py` | Adds + backfills `repos.index_semantics_version`. Backfill value is FROZEN as `_BACKFILL_VERSION = 1` — deliberately never imports the live `INDEX_SEMANTICS_VERSION`. Backfills by cadence (rows touched in the last 48h); untouched rows stay NULL → re-index once |
| `0003_multi_branch.py` | Multi-branch content dedup: adds `files.content_sha` (backfilled in-DB via `pgcrypto` `digest(...,'sha256')`, proven byte-identical to `indexer.hashing.content_sha`) and GIN-indexed `files.branches`; swaps `uq_files_repo_id_path` → `uq_files_repo_path_sha`; creates `repo_branches` seeded from the legacy `repos` stamp. Downgrade guards FIRST: refuses if any path has multiple content versions |
| `0004_semantic_chunks.py` | The semantic surface in the core chain (supersedes the retired gated `0002sem`/`versions_semantic`): extensions `lakebase_tokenizer` → `lakebase_vector` → `lakebase_text` with `CASCADE` (load-bearing: `lakebase_vector` declares a dependency on base `vector`), the `chunks` table (embedding dim from `app.config.SEMANTIC_EMBEDDING_DIM`, generated `ts` tsvector, `uq_chunks_file_id_chunk_index` — also the write-path index for per-file DELETE and CASCADE), `ix_chunks_embedding_ann` (`lakebase_ann` with explicit non-default `vector_cosine_ops`; rejects hnsw-style WITH params) and `ix_chunks_ts_bm25`. Idempotency guard: if `to_regclass('chunks')` exists (old gated path), only add `start_line`/`end_line` and drop the orphaned `alembic_version_semantic` |

## For AI Agents

### Working In This Directory
- **Chain ordering is strictly linear**: `0001 <- 0002 <- 0003 <- 0004`. A new revision sets `down_revision` to the current head; never branch the chain.
- **Migrations are historical facts.** They must never import mutable app constants — `0002` freezes its backfill value locally, and `models.py` explicitly forbids migrations importing `INDEX_SEMANTICS_VERSION`. The one sanctioned exception is `0004`'s use of `SEMANTIC_EMBEDDING_DIM`, which exists precisely so DDL and the `app/db/semantic.py` table can never drift.
- **Keep the `chunks` blindness intact.** `include_object` in `env.py` plus `chunks` living outside `Base.metadata` are two halves of one protection; removing either makes `make migration` emit destructive drops.
- **Extension-before-index ordering** is load-bearing in 0001 and 0004; downgrades never drop extensions (database-wide, potentially shared, and the 0004 preload prerequisite is irreversible).
- The `coalesce(default_branch,'HEAD')` used by the 0003 backfill must stay byte-identical to the compiler/semantic/`get_file` default-branch sites.
- On a project missing the managed `shared_preload_libraries` (`lakebase_vector,lakebase_text`), 0004's `CREATE EXTENSION` fails loudly with "must be loaded via shared_preload_libraries" — that error is the intended signal; see `docs/runbooks/semantic-enablement.md`.
- Run migrations via `make migrate` (`scripts/migrate.py`, injected connection, optional `ARGS=--apply-grants`); autogenerate via `make migration MSG="..."` against a disposable Lakebase branch (`scripts/ci_branch.py up`) — never against production. `env.py` deliberately raises rather than guessing a target.

### Testing Requirements
- `make test`: `tests/unit/test_migration_source.py` / `test_migration_source_semantic.py` (source-level revision-chain and no-app-import checks), `test_semantics_version_tripwire.py`.
- `make test-integration`: `tests/integration/test_migrations.py` (upgrade/downgrade against real Postgres; models ↔ chain parity), `test_content_sha_parity.py` (pgcrypto digest ≡ Python `content_sha`).

### Common Patterns
- Raw `op.execute()` for anything SQLAlchemy can't declare portably (extensions, generated columns, lakebase index access methods, backfill UPDATEs); `op.create_table`/`op.create_index` for the declarable rest.
- `revision`/`down_revision` as typed module constants; long module docstrings recording the decision evidence (Phase-0 gates, ground-truth capture dates) — keep that style.
- Downgrades restore the exact prior shape, and guard destructive cases loudly (0003's multi-branch check) instead of silently collapsing data.

## Dependencies

### Internal
- `app/db/models.py` (`Base.metadata` as autogenerate target), `app/db/client.py` (env-driven engine path), `app/config.py` (`SEMANTIC_EMBEDDING_DIM` in 0004), `scripts/migrate.py` (injects the connection and executes `app/db/grants.py` SQL), repo-root `alembic.ini`.

### External
`alembic` (`op`, `context`), `sqlalchemy`; Postgres extensions at runtime: `pg_trgm`, `pgcrypto`, `lakebase_tokenizer`/`lakebase_vector`/`lakebase_text`.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
