<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# app

## Purpose
The MCP server Databricks App: a FastMCP streamable-HTTP service exposing the indexed code-search corpus to agents. It registers four tools (`search_code`, `semantic_search`, `list_repos`, `get_file`) plus `/health` and `/ready` custom routes, and serves them over SQLAlchemy against Lakebase Postgres. Lexical search flows parser → compiler → grep/symbol legs (`app/query/`, `app/search/`); semantic search embeds the query via the workspace AI Gateway (`app/embed.py`) and fuses vector-ANN + BM25 legs with RRF. Payload builders live in `app/service.py` so a second Databricks App (`webui/`) reuses them in-process without importing this module's ASGI side effects.

## Key Files
| File | Description |
|------|-------------|
| `main.py` | FastMCP app: tool registration via `create_app()` factory, per-session `lifespan`, process-scoped engine singleton (`get_engine()`, `threading.Lock` + `atexit` dispose), `anyio` off-loop dispatch under a pool-sized `CapacityLimiter(5)`, `/health` (zero-DB) and `/ready` (`SELECT 1 FROM repos LIMIT 1` grant probe) |
| `service.py` | Shared payload builders (`search_code_payload`, `list_repos_payload`, `get_file_payload`, `clamp_limit`): merges grep + symbol legs into the zoekt-parity envelope, base64url pagination cursors (`encode_cursor`/`decode_cursor`, `CursorError` never swallowed), `commit:` prefix resolution against `repo_branches`, permalink-branch selection |
| `config.py` | `pydantic-settings` `Settings` with `CODE_SEARCH_` env prefix (timeouts, row limits, semantic tunables); unprefixed `LAKEBASE_ENDPOINT` via `validation_alias`; `SEMANTIC_EMBEDDING_DIM = 1024` single source of truth; `get_settings()` is `lru_cache`d once per process |
| `embed.py` | `EmbedFn` seam (texts → unit-normalized 1024-dim vectors) and `databricks_embedder`: POSTs to the AI Gateway MLflow embeddings route via the SDK's raw API client; lazy SDK import; per-batch count check (`EmbeddingCountMismatchError`) and dim check (`EmbeddingDimMismatchError`) fail loudly instead of misaligning vectors |
| `app.yaml` | Databricks App runtime config: `ln -sf . app` symlink so `app.` imports resolve at the uploaded working-dir root; shell-form command so `DATABRICKS_APP_PORT` expands; sets only `LAKEBASE_ENDPOINT` |
| `requirements.txt` | Deploy-time lockfile exported by `uv export --no-dev --no-hashes --no-emit-project` — regenerate, never hand-edit |
| `__init__.py` | Empty package marker |

## Subdirectories
| Directory | Description |
|-----------|-------------|
| [`db/`](db/AGENTS.md) | Lakebase engine factory (per-connection OAuth), ORM models, grant SQL builders, standalone `chunks` Core table |
| [`query/`](query/AGENTS.md) | Zoekt-style query parser (pure stdlib, seven fields) and AST → SQLAlchemy `Select` compiler |
| [`search/`](search/AGENTS.md) | Grep (per-line rescan), `sym:` definition search, hybrid semantic RRF search, shared `QueryTooBroadError` |
| [`alembic/`](alembic/AGENTS.md) | Migration env + the 0001→0004 core revision chain (also covers `versions/`) |

## For AI Agents

### Working In This Directory
- **The engine is a process-scoped module singleton, not lifespan-owned.** A FastMCP `lifespan` re-enters once per MCP session; building the engine there would re-pay Lakebase cold start and open N×5 pools. `get_engine()` in `main.py` is the only builder; the lifespan only references it and never disposes (that is `atexit`'s job).
- **Blocking work runs off the event loop.** Every tool body goes through `_dispatch` → `anyio.to_thread.run_sync` under `_DB_LIMITER` (sized to the 5-conn pool). Never run SQL or the Python `regex` rescan inline in an async handler.
- **Recoverable conditions are payload fields, never exceptions**: `truncated`, `query_too_broad`, `query_parse_error`, `regex_incompatible`, `regex_invalid`, `no_content_atom`, `zero_width_only_atoms`, `semantic_schema_missing`. Only genuinely unexpected faults reach `_dispatch`, which logs the traceback and re-raises. Envelope keys are additive and permanent — agents depend on them; never remove or reshape one.
- **`clamp_limit` gates every caller-supplied limit** (`<=0` → `row_limit`, `> max` → `max_row_limit`) before it reaches a builder.
- `main.py` aliases `service.*` builders (`_search_code_payload = service.search_code_payload`); tests monkeypatching collaborators must patch `service.*`, since function globals resolve in the defining module.
- Tools/routes are registered on a fresh `FastMCP` inside `create_app()` (a `streamable_http_app`'s session manager is single-use); do not decorate onto a module-global instance.
- `search_code`'s `branch`/`commit` params are sugar appended as query atoms (`_append_branch_atom` quotes and escapes only `"`); `semantic_search`'s `branch` is threaded straight to SQL, never into the natural-language query.
- Semantic flag-off (`CODE_SEARCH_SEMANTIC_ENABLED=0`) must stay a true no-op: no engine, no `chunks`, no SDK import.
- `config.py`'s `SEMANTIC_EMBEDDING_DIM` is imported by `app/db/semantic.py` and the 0004 migration DDL; a dim change must move all three together (a unit test tripwires model/dim drift).

### Testing Requirements
- `make test` — unit tests, no external deps: `tests/unit/test_main.py` (envelope shapes pinned to zoekt parity, engine singleton), `test_service.py`, `test_semantic.py`, `test_embed.py`, `test_semantic_schema.py`.
- `make test-integration` — needs Postgres (`PGHOST` set): `tests/integration/test_mcp_server.py` (streamable-HTTP e2e), `test_service.py`, `test_commit_search.py`, `test_semantic_rrf.py`.
- `make lint` — ruff check + format check + mypy.

### Common Patterns
- Tools return `str` via `json.dumps(payload)`; every tool routes through the single `_dispatch` choke-point which logs duration, signal fields, and limiter saturation.
- Double-checked `threading.Lock` lazy singletons (`get_engine` here, `get_embedder` in `app/search/semantic.py`) built off the event loop.
- Sentinel `_Unset` kwargs distinguish "argument omitted" (legacy envelope, no `next_cursor` key) from "explicitly `None`" (pagination page 1).
- Per-request `SET LOCAL statement_timeout` (int-coerced) inside `conn.begin()` on every raw SELECT, including the `/ready` probe.
- Lazy `databricks.sdk` imports inside function bodies so unit tests and flag-off paths never import the SDK.

## Dependencies

### Internal
- `indexer/` writes the corpus this app reads (shares `app.db` models, `app.embed`, `app/config.py` tunables).
- `webui/` (second Databricks App) imports `app.service` payload builders in-process.
- `scripts/migrate.py` runs the `app/alembic` chain and executes `app.db.grants` SQL.

### External
`mcp` (FastMCP), `starlette`, `anyio`, `uvicorn`, `sqlalchemy` (2.0), `psycopg`, `pydantic` / `pydantic-settings`, `databricks-sdk` (lazy), `pgvector`, `alembic`.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
