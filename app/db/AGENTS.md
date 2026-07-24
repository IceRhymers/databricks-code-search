<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# app/db

## Purpose
Database connectivity and schema truth for the code-search corpus. `client.py` is the one and only place that knows how to connect to Lakebase (Autoscaling API, per-connection OAuth token injection) or to plain local Postgres (CI/tests via `PGHOST`). `models.py` holds the SQLAlchemy 2.0 declarative models for the durable core (`repos` / `files` / `symbols` / `repo_branches` / `reference_edges`) whose `Base.metadata` drives Alembic autogenerate; `semantic.py` declares the `chunks` table in a deliberately separate `MetaData` so autogenerate never sees it; `grants.py` builds least-privilege GRANT SQL strings executed by `scripts/migrate.py`.

## Key Files
| File | Description |
|------|-------------|
| `client.py` | `create_db_engine()`: Lakebase-endpoint-wins-over-`PGHOST` dual-mode selection; Lakebase mode closes one `WorkspaceClient` over a `do_connect` handler that mints a fresh OAuth token as the password on every physical connect (never logged); server defaults `pool_size=5`, `pool_recycle=2700` (45 min, under the ~1h token TTL), `pool_pre_ping=True`; SDK import is lazy so the local path never touches it |
| `models.py` | ORM models + `INDEX_SEMANTICS_VERSION` (currently 4; bump on any indexing-meaning change — CI tripwires it). `File` is content-deduped per (repo_id, path, content_sha) with a `branches ARRAY(Text)` membership column; `RepoBranch` is the authoritative per-(repo, branch) CAS stamp; `repos`' own stamp columns and `files.commit` are deprecated/ambiguous — never add readers. `ReferenceEdge` (0005, epic #82) is a raw unresolved call/import edge, deliberately with NO FK to `symbols` (resolution happens at query time by name-join in a later child); FKs to `repos`/`files` only, both `ON DELETE CASCADE`. Declares the trgm + branches GIN indexes so autogenerate can't drift |
| `semantic.py` | Standalone Core `Table` for `chunks` (BigInteger PK, `Vector(SEMANTIC_EMBEDDING_DIM)` embedding, generated `ts` tsvector, nullable `start_line`/`end_line`) in its own `semantic_metadata` — a typed description only; the real DDL is owned by migration `0004` |
| `grants.py` | Pure SQL-string builders `build_app_grants` (read-only) / `build_job_grants` (CRUD + sequences, no DDL); identifiers validated against `^[A-Za-z0-9_-]+$` (1..63 chars) then psycopg-quoted. Execution lives in `scripts/migrate.py` |
| `__init__.py` | Re-exports `Base`, `File`, `Repo`, `Symbol`, `create_db_engine` |

## For AI Agents

### Working In This Directory
- **All Lakebase API knowledge stays in `client.py`.** The sole deviation (`w.postgres.generate_database_credential`, requiring databricks-sdk>=0.81.0) is confined here so a fallback to the Provisioned `w.database.*` API is a one-file change. Never mint or log tokens elsewhere.
- **Endpoint presence, not `PGHOST` absence, selects Lakebase.** A deployed App's `postgres` binding injects `PGHOST`/`PGUSER` without a usable password, so `LAKEBASE_ENDPOINT`/`endpoint=` wins even when `PGHOST` is set.
- **Do not add `chunks` to `Base.metadata`.** It lives in `semantic.py`'s own `MetaData`; pairing with the `include_object` filter in `app/alembic/env.py`, this is what stops autogenerate emitting `drop_table('chunks')` or spurious diffs of the hand-written vector/tsvector DDL.
- **`INDEX_SEMANTICS_VERSION` must never be imported by a migration** (migrations freeze their own constants — see `0002`). Bump it whenever `indexer/symbols.py`, `indexer/parse.py` chunking, or `indexer/languages.py` extraction changes meaning.
- `RepoBranch.last_indexed_commit` is the ONLY commit truth-source; `files.commit` is write-only and ambiguous under multi-branch dedup.
- **`reference_edges` must never gain a FK to `symbols`** (epic #82 rule) — resolution from `target_name` to a concrete symbol happens at query time by name-join, not via a stored FK. `tests/unit/test_reference_edge_model.py` and `tests/unit/test_migration_source.py::test_0005_no_symbol_fk` are standing tripwires; do not weaken them.
- The server's pool default (5) is paired with `app/main.py`'s `CapacityLimiter(5)`; the indexer passes `pool_size=` explicitly from its worker count. Changing one side without the other breaks the oversubscription guarantee.
- Grant changes are deploy-coupled: adding a privilege needs a `deploy.sh --apply-grants` re-run, not just a schema migrate.

### Testing Requirements
- `make test`: `tests/unit/test_db_client.py` (mode selection, URL building), `tests/unit/test_grants.py` (SQL text + identifier validation), `tests/unit/test_semantic_schema.py` (chunks table ↔ 0004 DDL consistency), `tests/unit/test_semantics_version_tripwire.py`.
- `make test-integration`: `tests/integration/test_db_client.py`, `test_migrations.py` (models ↔ migration-chain parity against real Postgres).

### Common Patterns
- Lazy `from databricks.sdk import WorkspaceClient` inside function bodies (never module scope) so local/unit paths stay SDK-free.
- Env-var fallbacks per keyword arg: `endpoint`→`LAKEBASE_ENDPOINT`, `host`→`LAKEBASE_HOST`, `database`→`LAKEBASE_DATABASE`, `user`→`LAKEBASE_USER` (then `current_user.me()`).
- Pure builders (grants) return SQL strings; execution and transaction handling live with the caller.
- Deprecations are annotated in-model with the owning migration (0002/0003) and an explicit "do not add readers/writers" instruction.

## Dependencies

### Internal
- `app/config.py` (`SEMANTIC_EMBEDDING_DIM` for the `chunks` embedding column).
- Consumed by `app/main.py`, `app/service.py`, `app/query/compiler.py`, `app/search/*`, `app/alembic/env.py`, `indexer/`, `scripts/migrate.py`.

### External
`sqlalchemy` (2.0 declarative + `event`), `psycopg` (`sql.Identifier` quoting), `pgvector.sqlalchemy` (`Vector`), `databricks-sdk` (lazy, Lakebase mode only).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
