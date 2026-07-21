<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# webui

## Purpose
The second Databricks App in this bundle (issue #35): a browser-facing search UI over the same Lakebase corpus the MCP app serves. `main.py` is a FastAPI backend that imports the MCP app's `app.*` stack in-process — the same `app.service` payload builders (`search_code_payload`, `get_file_payload`, `list_repos_payload`, `semantic_search_payload`), config, and DB client — so search behavior is exactly the MCP tools' behavior, wired to HTTP GET routes instead of MCP tool calls. It deliberately owns its OWN engine singleton, `CapacityLimiter`, and off-loop dispatch pattern rather than importing `app.main` (which would build the FastMCP/Starlette app as an import side effect); the two apps are independent processes, each with its own connection pool. The React/Vite SPA in `frontend/` is served from the committed `frontend/dist/` build via a `SPAStaticFiles` mount at `/` (after all API routers, with `index.html` fallback for client-side routes but JSON 404s for unregistered `/api/*` paths).

## Key Files
| File | Description |
|------|-------------|
| `main.py` | FastAPI backend: `/health`, `/ready` (grant-oracle probe `SELECT 1 FROM repos LIMIT 1`), `/api/search` (keyset-cursor pagination — `cursor` always passed explicitly so every envelope carries `next_cursor`), `/api/file` (optional `branch` param), `/api/repos`, `/api/semantic/status` (flag-only, zero-DB), `/api/semantic` (hybrid RRF), security-headers middleware, SPA mount. `get_engine`/`get_settings` are FastAPI `Depends(...)` seams so tests override them via `app.dependency_overrides`. |
| `app.yaml` | Databricks App runtime config: `uv run --frozen uvicorn main:app` in shell form (so `$DATABRICKS_APP_PORT` expands), `LAKEBASE_ENDPOINT` env, `UV_LINK_MODE=copy` (cross-device hardlink fix in the Apps sandbox). No `ln -sf` symlink trick like `app/app.yaml` — `app.*` comes from the installed wheel. |
| `pyproject.toml` | uv manifest that makes the Apps runtime pick the uv path (Python 3.12): depends on `databricks-code-search[webui]` resolved from a RELATIVE `[tool.uv.sources]` path to the staged wheel in `wheels/`; `requires-python = ">=3.12,<3.13"` (upper bound is load-bearing — see file comments); `package = false`. |
| `.python-version` | Pins the interpreter minor (3.12) that `uv.lock` is resolved against. |
| `__init__.py` | Empty package marker (lets mypy/tests import `webui.main`). |
| `app.whl` / `uv.lock` | NOT committed — both are staged by `make webui-wheel` before every deploy (uv.lock is gitignored repo-wide). |

## Subdirectories
| Directory | Description |
|-----------|-------------|
| `frontend/` | React/Vite/TypeScript SPA source, config, and committed production build — see `frontend/AGENTS.md`. |
| `wheels/` | Generated wheel storage (only `.gitkeep` is committed). `make webui-wheel` runs `uv build --wheel` at the repo root, stages the versioned wheel here (`databricks_code_search-0.1.0-py3-none-any.whl` — the filename is tracked in `pyproject.toml`'s path source, update both together on a version bump), and regenerates `webui/uv.lock` against it. `.gitignore` carries an explicit `!webui/wheels/*.whl` negation because DABs source sync respects `.gitignore` — an ignored wheel silently fails to upload and the app crashes on import. |

## For AI Agents

### Working In This Directory
- **The wheel mechanism is sync-critical.** `main.py` imports `app.*` from the wheel installed via `pyproject.toml`, not from sibling source. `scripts/deploy.sh` runs `make webui-wheel` before `bundle deploy`; never hand-place or commit wheels, and never add a `requirements.txt` here — "requirements.txt always takes precedence" on the Apps runtime and would force pip + Python 3.11, on which the wheel (`requires-python >=3.12`) refuses to install (see PR #42's five stacked failures documented in `pyproject.toml`/`app.yaml` comments).
- **`frontend/dist/` is committed** — never hand-edit it; rebuild with `make webui-build` and commit the result. There is no build-time check that `dist/` matches `frontend/src/`.
- **Auth model:** standard Databricks Apps workspace auth — any identity with `CAN_USE` on the app resource can use it. No OAuth app connection (that prerequisite is specific to the MCP app's `/mcp` transport). Never add auth logic here.
- **Read-only DB access:** the app SP gets `SELECT`/`USAGE` only (`scripts/migrate.py --apply-grants`). Routes must never write; `/ready` is the grant oracle (missing grant → 503).
- **Error contract:** recoverable conditions (parse errors, semantic disabled/schema-missing) are payload fields, never exceptions — only `CursorError`, `DataError` (NUL-byte inputs → 400), and backend faults (→ 502, generic body, detail logged server-side only) become HTTP errors. Preserve the no-leak policy: never echo raw DB/SDK errors in response bodies.
- New routes go in `create_app()` BEFORE the SPA mount; blocking DB work must go through `_run_blocking` (pool-sized limiter).

## Testing Requirements
- `uv run pytest tests/unit/test_webui_main.py` — backend route tests (dependency-override seams), part of `make test`.
- `tests/integration/test_webui_semantic.py` — semantic route integration coverage (`make test-integration`).
- `make lint` runs `mypy app indexer webui` — this package is type-checked; keep annotations complete.
- `make webui-test` — frontend vitest suite (CI `webui` job; advisory, not wired into `make test`).

## Common Patterns
- Route handlers are module-level `async def` functions registered functionally (`app.get(...)(handler)`) in `create_app()`.
- `Annotated[..., Depends(...)]` / `Annotated[..., Query(...)]` for all injection and validation; `limit` goes through `service.clamp_limit`.
- Heavy "why" comments citing issue numbers and mirrored `app/main.py` line ranges — keep that style when editing.

## Dependencies

### Internal
- `app.service`, `app.config` (`Settings`, `get_settings`), `app.db.client.create_db_engine` — all via the staged wheel at deploy time, via the repo checkout in local dev/tests.

### External
- `fastapi`/`starlette` (the wheel's `webui` extra), `anyio`, `sqlalchemy` (+psycopg), `uvicorn`; deployed via DABs (`resources/webui.yml`, `source_code_path: ../webui`).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
