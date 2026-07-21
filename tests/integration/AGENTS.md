<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# tests/integration/

## Purpose
Integration and e2e tests that execute real SQL through real engines. This project is **Lakebase-only**: there is no local Postgres in the development loop, and the semantic suite depends on beta operators (`lakebase_ann`'s `<=>` ordered-index scan, `lakebase_bm25`'s `ts <@> to_bm25query(...)`) that exist in no Postgres image. The only integration gate is `.github/workflows/ci-lakebase.yml` (gated on repo var `CI_LAKEBASE_ENABLED`): `scripts/ci_branch.py up` forks an ephemeral copy-on-write Lakebase branch and exports `LAKEBASE_ENDPOINT`/`LAKEBASE_DATABASE` → `scripts/migrate.py` runs `upgrade head` (including the semantic `0004` revision) → `make test-integration` → `ci_branch.py down` purges the branch in an `always()` step (2h TTL backstop). `PGHOST` stays unset so `app/db/client.py` takes the Lakebase OAuth path. Locally these modules are validated by lint/type-check + `pytest --collect-only`, not execution — several docstrings say so explicitly, and some older docstrings still describe "CI's Postgres service container"/"standard PG* env"; the engine factory does retain a `PGHOST` local-mode path, but the project's actual gate is the Lakebase branch.

Within a run, isolation is per-test-module: every fixture creates a uniquely-named throwaway schema (`f"{PREFIX}_{uuid4().hex[:12]}"`), builds DDL there, and drops it in a `finally`. The branch isolates concurrent CI runs; the schema isolates tests. All modules are marked `integration` except `test_mcp_server.py` (`e2e`); both run under `make test-integration` (`pytest -m "integration or e2e"`). No conftest — fixtures are module-local by convention.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Empty package marker. |
| `test_commit_search.py` | `commit:` git-hash search: resolution + scoped equivalence; deliberately indexes matching content on a NON-default branch to prove `commit:` suppresses the implicit default-branch conjunct; prefix-collision cases; PGOPTIONS idiom. |
| `test_content_sha_parity.py` | Phase-0 hard gate: Postgres `pgcrypto` `digest(...,'sha256')` and `indexer.hashing.content_sha` must agree (else re-index would silently duplicate the corpus); also proves pgcrypto is creatable by the migrator's connection. |
| `test_db_client.py` | Engine factory end-to-end against a Lakebase branch; real DDL fidelity asserted via `pg_catalog`/`information_schema` after `create_all()`, not metadata reflection. |
| `test_grep.py` | Grep search: query → executed SQL → line matches; throwaway schema with `pg_trgm` + trgm GIN indexes; function-scoped `seeded` fixture (timeout tests would leak a shared connection's settings). |
| `test_mcp_server.py` | **e2e**: FastMCP server over streamable HTTP via `httpx.ASGITransport` + `asgi_lifespan.LifespanManager`. Two load-bearing seams: `PGOPTIONS` set before the server engine is built (+ reset of `app.main._engine`), and the DNS-rebinding constraint (client must use port-bearing `http://localhost:8000` base URL). |
| `test_migrations.py` | Core migration chain (via `scripts/migrate.py` loaded by path) in a throwaway schema with an injected connection and pinned Alembic version table; grants enforcement via `SET ROLE` to NOLOGIN roles on the SAME superuser connection; `compare_metadata` drift check. |
| `test_query_compiler.py` | Query compiler AST → executed SQL rows; module-scoped `seeded` corpus (read-only tests, safe to share); includes `EXPLAIN` determinism assertions. |
| `test_semantic_rrf.py` | Hybrid RRF against the production `lakebase_ann`/`lakebase_bm25` operators: fusion plumbing (FULL OUTER JOIN + `1/(k+rank)`), real BM25 ranking, ANN index usage via EXPLAIN; `chunks` built with DDL identical to `0004`; fails loudly if the access methods are absent. |
| `test_service.py` | `search_code_payload` keyset-cursor pagination (issue #35): engine-per-call service, so the PGOPTIONS idiom (not a held-open connection) makes the schema visible to every pooled connection. |
| `test_store.py` | `indexer.store.index_repo` upsert/sweep/rollback via the injected-connection seam; multi-branch scenarios: shared/divergent content across branches, per-branch CAS, empty-seen-set guard. |
| `test_store_chunk_writer.py` | `chunk_writer` seam end-to-end: chunks ride the same `conn.begin()` as the file row and cascade-delete on sweep (FK ON DELETE CASCADE); `chunks` table built with raw DDL (cross-`MetaData` FK can't be sorted by `create_all`). |
| `test_symbols_search.py` | Symbol search (`sym:`): query → executed SQL → symbol defs; function-scoped `seeded` fixture (timeout + determinism tests need a clean corpus). |
| `test_webui_semantic.py` | webui `/api/semantic` route on a Lakebase branch: dependency wiring, HTTP status/shape, not-migrated/disabled passthrough (ranking itself is `test_semantic_rrf.py`'s job); embedder seam monkeypatched, PGOPTIONS set before the overridden engine is built. |

## For AI Agents

### Working In This Directory
- Mark tests `@pytest.mark.integration`, or `@pytest.mark.e2e` only for in-process streamable-HTTP tests driving the real ASGI app. `--strict-markers` is on; unmarked tests run under no make target.
- New modules must follow the throwaway-schema idiom (copy the closest sibling and say so in your docstring): unique schema name from a module `SCHEMA_PREFIX` + uuid, `CREATE SCHEMA` + `SET search_path` (+ `CREATE EXTENSION pg_trgm` where trgm indexes matter), `Base.metadata.create_all` on that connection, and `DROP SCHEMA ... CASCADE` + `engine.dispose()` in a `finally`.
- Pick the right visibility mechanism: a single held-open connection with `SET search_path` works only when the code under test receives that connection (store/migrations style). If the code takes an `Engine` and opens its own connections (`service`, MCP server, webui routes), set `os.environ["PGOPTIONS"] = "-c search_path=<schema>,public"` **before** creating the engine, and warm the pool before another fixture mutates the env var.
- Fixture scope is a correctness decision, documented per module: module-scoped only for read-only corpora (`test_query_compiler.py`); function-scoped when tests set `statement_timeout_ms`, insert large rows, or rely on clean counters.
- Never point these tests at the production Lakebase branch, and never migrate the CI project's `production` branch — forks inherit installed extensions, and `test_no_vector_extension_installed` (in `test_migrations.py`) asserts the core migration leaves no vector-family extension installed.
- Real embeddings are never computed: `test_webui_semantic.py` monkeypatches `app.search.semantic.get_embedder` to a fake returning `SEMANTIC_EMBEDDING_DIM`-wide vectors.

### Testing Requirements
```bash
# Bring up an ephemeral branch (needs Databricks auth to the CI Lakebase project):
uv run python scripts/ci_branch.py up --project code-search-ci --branch <slug>   # prints LAKEBASE_ENDPOINT=... LAKEBASE_DATABASE=...
export LAKEBASE_ENDPOINT=... LAKEBASE_DATABASE=...                               # PGHOST must stay unset
uv run python scripts/migrate.py
make test-integration                       # uv run pytest -m "integration or e2e"
uv run python scripts/ci_branch.py down --project code-search-ci --branch <slug> # idempotent, never fails

# Local validation without a branch (the normal pre-push check for this suite):
uv run pytest tests/integration --collect-only
make lint
```

### Common Patterns
- Module-local fixtures yielding `NamedTuple`s (`Seeded`, `Migrated`) bundling connection/engine + seeded ids; no conftest.
- Seeding through SQLAlchemy `insert()` on `app.db.models` (`Repo`, `File`, `RepoBranch`, `Symbol`), content hashed with `indexer.hashing.content_sha` so parity holds with production writes.
- `scripts/migrate.py` is loaded by file path with `importlib` (scripts/ is not a package) and driven through its injected-connection entry point.
- DDL fidelity is asserted against `pg_catalog`/`information_schema`, and index/plan behavior via `EXPLAIN`, not just ORM reflection.
- Grants enforcement uses `SET ROLE` on the same superuser connection — a second engine would reconnect as superuser and bypass the grants under test.

## Dependencies
- Internal: `app.db.client.create_db_engine` (Lakebase OAuth engine factory), `app.db.models` / `app.db.grants`, `app.service`, `app.main` (ASGI app + `_engine` singleton), `app.search.*` (grep, symbols, semantic, errors), `app.config` (`Settings`, `SEMANTIC_EMBEDDING_DIM`), `indexer.store` / `indexer.hashing`, `webui/main.py`, `scripts/migrate.py`, `scripts/ci_branch.py` (lifecycle, run from CI not from tests).
- External: `pytest`, `pytest-asyncio`, `sqlalchemy`, `psycopg`, `alembic` (command + autogenerate compare), `httpx` + `asgi-lifespan` + `mcp` client (e2e module), `databricks-sdk` (OAuth inside `create_db_engine`); server-side: Lakebase Postgres with `pg_trgm`, `pgcrypto`, and — for semantic modules — preloaded `lakebase_vector,lakebase_text`.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
