<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# databricks-code-search

## Purpose
An agent-facing code search service on Databricks. A scheduled serverless job indexes
GitHub repositories into Lakebase Postgres; two Databricks Apps read that corpus — an MCP
server exposing zoekt-style search to agents over streamable HTTP, and a browser-facing
web UI. Everything deploys as one Databricks Asset Bundle.

## Key Files
| File | Description |
|------|-------------|
| `README.md` | Primary human documentation: architecture, query language, deploy, client setup |
| `Makefile` | Every workflow entry point (`make help` lists all); deploy, test, lint, diagrams |
| `config.yaml` | Single source of truth for the indexed repo set; template ships with nothing selected |
| `databricks.yml` | Bundle root: variables (`max_repos`, `index_schedule`), targets, sync rules |
| `pyproject.toml` | uv-managed project; Python 3.12+; dependency groups per component |
| `alembic.ini` | Alembic config; migration env lives in `app/alembic/` |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `app/` | MCP server app: FastMCP server, query parser/compiler, search, DB client, migrations (see `app/AGENTS.md`) |
| `indexer/` | Indexing job: config resolution, GitHub fetch, parsing, symbols, storage (see `indexer/AGENTS.md`) |
| `webui/` | Second Databricks App: FastAPI backend + React/Vite frontend (see `webui/AGENTS.md`) |
| `tests/` | Unit and integration suites (see `tests/AGENTS.md`) |
| `scripts/` | Deploy pipeline, smoke test, migration runner, CI branch lifecycle (see `scripts/AGENTS.md`) |
| `docs/` | Runbooks and Graphviz diagrams (see `docs/AGENTS.md`) |
| `resources/` | Bundle resource declarations: apps, job, Lakebase, grants (see `resources/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- Use `uv` for everything Python (`make install` = `uv sync --all-groups --all-extras`); never pip directly.
- This project is **Lakebase-only** — there is no local/CI Postgres image. Integration
  tests need an ephemeral Lakebase branch (`scripts/ci_branch.py up`).
- `config.yaml` is a published template: keep it pointing at placeholder values, never a
  real account. An empty selector set must keep failing fast (`connection selects nothing`).
- The committed PNGs under `docs/diagrams/` are generated — edit the `.dot` sources and
  run `make diagrams`.
- `webui/frontend/dist/` is a committed production build; rebuild with `make webui-build`
  (needs Node), don't hand-edit.

### Testing Requirements
- `make test` — unit + observability markers; no external dependencies, no database.
- `make test-integration` — needs `LAKEBASE_ENDPOINT`/`LAKEBASE_DATABASE` from an ephemeral branch.
- `make lint` — `ruff check` + `ruff format --check` + `mypy app indexer webui`; all are repo gates.
- `make webui-test` — vitest; advisory, not a gate.

### Common Patterns
- Seams for testability: the DB engine, the embedder (`app/embed.py`), and the GitHub
  client are injected/lazy so unit tests never touch the network or database.
- Recoverable conditions are payload fields (`query_parse_error`, `truncated`, …), not
  raised errors — agents react without a failed tool call.
- Env config uses the `CODE_SEARCH_` prefix (`app/config.py`), e.g.
  `CODE_SEARCH_SEMANTIC_ENABLED=0`.

## Dependencies

### External
- Databricks: Asset Bundles, Apps, serverless jobs, Lakebase Postgres, AI Gateway (embeddings)
- FastMCP (MCP server), FastAPI (webui), SQLAlchemy + Alembic, tree-sitter (symbols)
- uv, ruff, mypy, pytest; React + Vite (frontend)

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
