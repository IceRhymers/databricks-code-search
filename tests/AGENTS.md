<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# tests/

## Purpose
Two-tier pytest suite for the code-search project. `tests/unit/` is hermetic (no network, no database, no Databricks SDK instantiation — every I/O boundary is faked or injected) and runs everywhere, including plain CI (`ci.yml`). `tests/integration/` exercises real SQL against a real Lakebase Postgres branch and is CI-only in practice (`ci-lakebase.yml`): this project is Lakebase-only, and its semantic suite depends on the `lakebase_ann` (`<=>` ordered-index scan) and `lakebase_bm25` (`ts <@> to_bm25query(...)`) operators that no local Postgres image provides. There is deliberately **no `conftest.py` anywhere in this tree** — every test module is self-contained and defines its own fixtures, so nothing is shared implicitly between modules.

Pytest is configured in `pyproject.toml` under `[tool.pytest.ini_options]`: four registered markers (`unit`, `integration`, `e2e`, `observability`), `--strict-markers` (an unregistered or missing marker fails collection/selection), `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `pythonpath = ["."]`.

Marker → target mapping:

| Marker | Meaning | Runs under |
|--------|---------|-----------|
| `unit` | Pure unit tests, no external dependencies | `make test` (`pytest -m "unit or observability"`) |
| `observability` | Logging choke-point / fault-not-swallowed / saturation-signal tests (currently in `tests/unit/test_main.py`) | `make test` |
| `integration` | Multi-module tests executing real SQL against a Lakebase branch | `make test-integration` (`pytest -m "integration or e2e"`) |
| `e2e` | In-process streamable-HTTP tests driving the real ASGI app (currently `tests/integration/test_mcp_server.py`) | `make test-integration` |

The Lakebase-only integration story (see `.github/workflows/ci-lakebase.yml` and `docs/runbooks/ci-lakebase.md`): each CI run forks an ephemeral copy-on-write Lakebase branch via `scripts/ci_branch.py up` (prints `LAKEBASE_ENDPOINT`/`LAKEBASE_DATABASE` lines for `$GITHUB_ENV`), runs `scripts/migrate.py` (`upgrade head`, which includes the semantic `0004` revision), then `make test-integration`, then `scripts/ci_branch.py down` in an `always()` step (idempotent, never fails the build; a 2h TTL is the safety net for cancelled runs). `PGHOST` stays unset so `app/db/client.py` takes the Lakebase OAuth path. Within the run, fixtures build their own throwaway schemas — the branch is the isolation primitive between runs, the schema is the isolation primitive between tests.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Empty package marker (needed because `pythonpath = ["."]` imports tests as a package; allows same-named modules like `test_db_client.py` in both subpackages). |
| `unit/` | Hermetic unit tests — see `unit/AGENTS.md`. |
| `integration/` | Lakebase-backed integration + e2e tests — see `integration/AGENTS.md`. |

Note: there is no top-level `conftest.py`. If you find yourself wanting one, check the sibling modules first — the established convention is copying/adapting a named fixture idiom (documented in each module's docstring) rather than centralizing.

## For AI Agents

### Working In This Directory
- Put hermetic tests in `tests/unit/` with `@pytest.mark.unit`; DB-executing tests in `tests/integration/` with `@pytest.mark.integration` (or `e2e` for full ASGI/streamable-HTTP flows). `--strict-markers` means a typo'd marker fails immediately, and an unmarked test is silently skipped by both `make test` and `make test-integration` — always mark.
- Unit tests must never touch the network, a database, or instantiate the Databricks SDK. The codebase provides seams for this: injected `Connection`/`Engine` fakes, injected `client` (fake `WorkspaceClient`), monkeypatched `app.search.semantic.get_embedder`, `httpx.MockTransport`, FastAPI `dependency_overrides`, injected enumerators/loaders. Verify the exact seam names in the module you are extending.
- Integration tests must create a uniquely-named throwaway schema, do all work inside it, and `DROP SCHEMA ... CASCADE` + `engine.dispose()` in a `finally`/teardown. When the code under test opens its own pooled connections (an `Engine`-taking service), set `os.environ["PGOPTIONS"] = "-c search_path=<schema>,public"` **before** building the engine.
- Async tests need no decorator: `asyncio_mode = "auto"`.

### Testing Requirements
```bash
make test                # uv run pytest -m "unit or observability"  (no external deps)
make test-integration    # uv run pytest -m "integration or e2e"     (needs LAKEBASE_ENDPOINT/LAKEBASE_DATABASE from scripts/ci_branch.py up + scripts/migrate.py)
uv run pytest tests/unit/test_grep.py -k byte_offsets   # single module / test
uv run pytest tests/integration --collect-only          # local validation idiom for CI-only integration tests
make lint                # ruff check + format check + mypy (tests are ruff-checked)
```

### Common Patterns
- Module docstrings are load-bearing: each one states what is covered, which sibling module's fixture idiom it mirrors, and why. Keep this convention when adding modules.
- Fixtures are module-local, never shared via conftest; NamedTuple fixture results (`Seeded`, `Migrated`) bundle a connection with seeded row ids.
- CI-only integration modules record in their docstring that they were validated locally by lint/type-check + `--collect-only`, not execution.

## Dependencies
- Internal: `app/*` (config, service, db.client/models/grants, search.*, main, embed), `indexer/*` (job, store, chunk_store, parse, fetch, resolve, symbols, branches, repo_config, hashing), `webui/main.py`, `scripts/` (`migrate.py`, `ci_branch.py`, `smoke.py` — loaded by path, not importable).
- External (dev group in `pyproject.toml`): `pytest`, `pytest-asyncio`, `httpx`, `asgi-lifespan`; plus runtime deps exercised directly: `sqlalchemy`, `psycopg`, `alembic`, `mcp` (client), `tree_sitter` (via indexer). `databricks-sdk` is only ever faked in unit tests and only used for OAuth via `app/db/client.py` in integration.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
