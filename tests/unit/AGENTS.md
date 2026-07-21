<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# tests/unit/

## Purpose
Hermetic unit tests: no network, no database, no Databricks SDK instantiation. Every I/O boundary is replaced through an explicit seam — fake `Connection`/`Engine` objects, injected fake `WorkspaceClient`-shaped clients, `httpx.MockTransport`, FastAPI `dependency_overrides`, monkeypatched module attributes — so the suite runs anywhere with `make test` and is the default CI gate (`ci.yml`). Alongside behavioral tests, several modules are *static-source tripwires* that read source files and pin hand-edited invariants (migration revision chains, token redaction, `INDEX_SEMANTICS_VERSION` bumps) that an accidental regeneration could silently undo. All tests are marked `@pytest.mark.unit` except three `@pytest.mark.observability` tests at the bottom of `test_main.py`; both markers run under `make test`. There is no conftest — fixtures and fakes are defined in each module.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Empty package marker. |
| `test_branches.py` | `indexer.branches.resolve_branches`: glob matching, dedup, cap; empty globs → default branch only. |
| `test_chunk_store.py` | `indexer.chunk_store.write_chunks` statement shape via a fake `Connection` recording `execute` calls; delete-then-insert, and proof no embedding call happens here. |
| `test_chunking.py` | `indexer.parse.iter_chunks` chunking behavior (issue #14 Phase 1). |
| `test_ci_branch.py` | `scripts/ci_branch.py` lifecycle with the SDK fully faked; pins that teardown NEVER raises and every create carries a TTL (leak protection for cancelled CI runs). |
| `test_db_client.py` | Engine factory local (`PGHOST`) mode builds without instantiating the SDK; ORM models expose exactly the durable-core columns, constraints, and GIN indexes. |
| `test_embed.py` | `app.embed`: batching, retry, dim-mismatch, lazy SDK import — every test injects a fake `client`, `databricks.sdk` is never imported. |
| `test_fetch.py` | `indexer.fetch` via `httpx.MockTransport` + in-memory tarballs. |
| `test_grants.py` | Least-privilege grant builders: presence AND absence of privileges per role; hostile identifiers rejected before SQL is produced. |
| `test_grep.py` | Grep line extraction + matcher building (`extract_line_matches`, `_build_matchers`); byte-offset invariant `line_text.encode("utf-8")[s:e] == matched`. |
| `test_job.py` | `indexer.job`: `read_github_token` + orchestration with every I/O boundary faked (fake `WorkspaceClient`, injected `config_loader`, etc.). |
| `test_job_redaction.py` | GitHub-token redaction proof + source-level tripwire (two independent guards). |
| `test_languages.py` | Language/symbol source-of-truth maps: `SYMBOL_KINDS` ⊆ `EXT_TO_LANG` values, no orphans. |
| `test_main.py` | MCP server payload builders (`_search_code_payload` / `_list_repos_payload` / `_get_file_payload`), error mapping, and the `observability`-marked logging choke-point tests; fake engine/connection + fake `GrepResult`. |
| `test_migration_source.py` | Static source assertions on the linear migrations: fixed revision ids, `down_revision` chain, `pg_trgm` invariants. |
| `test_migration_source_semantic.py` | Static source assertions on the semantic `0004` revision (its DDL only runs on a Lakebase branch, so source reads are the unit-tier guard). |
| `test_parse.py` | `indexer.parse.iter_source_files` against a temp directory tree. |
| `test_placeholder.py` | Trivial `assert True` placeholder. |
| `test_query_compiler.py` | Query compiler AST → SQLAlchemy `Select`; statements rendered via `stmt.compile(dialect=postgresql.dialect())` and asserted on operator fragments + bound params. |
| `test_query_corpus_parity.py` | Cross-language parity gate: asserts the same verdicts as the TS suite over the shared `webui/frontend/src/utils/queryModel.corpus.json` (issue #47). |
| `test_query_parser.py` | Zoekt query parser (issue #8). |
| `test_repo_config.py` | `indexer.repo_config` schema/parsing/canonicalisation; workspace read seam faked with `io.BytesIO`; pins the import-light property (must NOT import `indexer.job`). |
| `test_resolve.py` | `indexer.resolve` filtering/dedup/fail-fast with injected enumerators (`httpx.Client` passed as `None`). |
| `test_semantic.py` | `app.search.semantic`: flag-off no-op proven against a poisoned engine, RRF SQL shape, vector-literal formatting via `repr`. |
| `test_semantic_schema.py` | Isolation + embedding-dim tripwires for the semantic `chunks` schema (model swap with a different dim fails at test time). |
| `test_semantics_version_tripwire.py` | CI tripwire: extraction-semantics changes must bump `INDEX_SEMANTICS_VERSION`. |
| `test_service.py` | Keyset-cursor pagination in `app/service.py`: pure cursor encode/decode + pagination-mode gating with fake engine/`GrepResult` (real keyset SQL lives in integration). |
| `test_smoke.py` | Pure predicate functions in `scripts/smoke.py`, loaded by file path (scripts/ is not a package). |
| `test_store_chunk_writer.py` | `indexer.store`'s optional `chunk_writer` param with a hand-rolled fake `Connection`: writer called inside the same `conn.begin()` with `(repo_id, file_id, pf)`. |
| `test_symbols.py` | `indexer.symbols.extract_symbols` across the V1 languages; nested symbols and the Python-vs-JS/TS method-kind asymmetry. |
| `test_symbols_search.py` | `sym:` atom walker (pure) + rendered step-2 projection SQL; the composed two-query path is integration's job. |
| `test_webui_main.py` | Route-level tests of the webui FastAPI backend via `app.dependency_overrides` of the `get_engine`/`get_settings` dependencies. |

## For AI Agents

### Working In This Directory
- Mark every test `@pytest.mark.unit` (or `@pytest.mark.observability` for logging choke-point tests) — `--strict-markers` is on, and unmarked tests run under no make target.
- Never touch network, filesystem outside tmp fixtures, a real database, or `databricks.sdk` at import or run time. Use the existing seams: inject a fake `client` (a `WorkspaceClient` stand-in, e.g. `test_embed.py`'s `_FakeApiClient`), a fake `Connection` that records `execute` calls (`test_chunk_store.py`, `test_store_chunk_writer.py`), a fake engine/connection + fake `GrepResult` (`test_main.py`, `test_service.py`), `monkeypatch.setattr(semantic, "get_embedder", ...)` (`test_semantic.py`), `httpx.MockTransport` (`test_fetch.py`), `io.BytesIO` for the workspace read seam (`test_repo_config.py`), FastAPI `dependency_overrides` (`test_webui_main.py`), and injected enumerators/loaders (`test_resolve.py`, `test_job.py`).
- To assert SQL without a DB, render statements with `stmt.compile(dialect=postgresql.dialect())` and assert operator fragments + bound parameters — never full-string equality.
- `Settings` instances are constructed explicitly in tests (see `_cfg()` helpers) so the real environment is never read.
- If you change extraction semantics in the indexer, `test_semantics_version_tripwire.py` will fail until you bump `INDEX_SEMANTICS_VERSION`; if you regenerate migrations, `test_migration_source*.py` will catch clobbered hand-edits. Fix the code/version, don't loosen the tripwire.

### Testing Requirements
```bash
make test                                   # uv run pytest -m "unit or observability"
uv run pytest tests/unit -m unit            # unit only
uv run pytest tests/unit/test_embed.py -v   # one module
make lint                                   # ruff + mypy must also pass
```

### Common Patterns
- Module-local fixtures and fake classes (no conftest); fakes are small hand-rolled classes with recording behavior, not MagicMock graphs.
- Docstrings name the seam being used and cross-reference the sibling module whose idiom they mirror.
- Static-source tripwire pattern: glob the real source files, read text, assert pinned fragments (`test_migration_source.py`, `test_job_redaction.py`).
- Path-loading pattern for `scripts/`: `importlib` by file path (`test_smoke.py`, mirroring `tests/integration/test_migrations.py`).

## Dependencies
- Internal: `app.*` (config, embed, main, service, db.client/models/grants, search.grep/semantic/symbols/errors, query parser/compiler), `indexer.*` (branches, chunk_store, parse, fetch, resolve, symbols, job, repo_config, store, hashing, languages), `webui/main.py`, `scripts/ci_branch.py`, `scripts/smoke.py`, and `webui/frontend/src/utils/queryModel.corpus.json` (shared parity corpus).
- External: `pytest`, `pytest-asyncio` (auto mode), `httpx` (MockTransport + test client), `sqlalchemy` (compile-only, postgresql dialect). No live `databricks-sdk`, `psycopg` connection, or network use.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
