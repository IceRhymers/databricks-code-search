<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# app/search

## Purpose
The impure serve-side execution layer sitting between the pure `app/query/` seams and the payload builders in `app/service.py`. `grep.py` runs the compiler's candidate query, then streams content one row at a time and rescans it with Python `re` for per-line zoekt-shaped highlights; `symbols.py` answers `sym:` queries with actual definitions (name/kind/line) the highlight-driven grep path cannot return; `semantic.py` is the natural-language companion — vector-ANN + BM25 legs over `chunks` fused with reciprocal-rank fusion (RRF, k=60) via the Lakebase `lakebase_ann`/`lakebase_bm25` operators; `errors.py` is the shared timeout → `QueryTooBroadError` mapper.

## Key Files
| File | Description |
|------|-------------|
| `grep.py` | `grep_search(conn, query, ...) -> GrepResult`: compile → candidate ids → content fetch with `yield_per=1` (one-row server-side cursor; bare `stream_results` does NOT bound memory) → `extract_line_matches` per line. Bounds: transaction-local `statement_timeout` (cancel → `QueryTooBroadError`), aggregate `max_content_bytes` cap (trip → `truncated`/`"byte_cap"`). Emits UTF-8 line-local half-open `byte_ranges`, `regex_incompatible` (NOT-RE2 degradation), the issue-#31 shape flags `no_content_atom` / `zero_width_only_atoms` (mutually exclusive by construction; zero-width proof via guarded `re._parser.getwidth()`), and `FileCursor`-based pagination (`_Unset` sentinel: cursor kwarg supplied at all = pagination mode) |
| `symbols.py` | `symbol_search(conn, query, ...) -> SymbolResult`: deliberately TWO queries — the full-AST `compile_query` picks eligible file ids, then a pure projection returns matching `symbols` rows. One joined query would let SQLAlchemy auto-correlate the compiler's `sym:`/`repo:` subqueries against outer rows (silent mis-scope). No `sym:` atom → short-circuit `no_symbol_atom=True` with zero DB hits. Pure SQL: none of grep's NOT-RE2/CPU caveats apply |
| `semantic.py` | `_semantic_search_payload`: flag-off short-circuit FIRST (no engine/SDK), then a `to_regclass('chunks')` schema probe (→ `semantic_schema_missing` payload, not a 500), embed OUTSIDE any transaction, then the RRF query. `build_hybrid_rrf_sql`: two rank CTEs whose inner `ORDER BY <metric> LIMIT :topk` must stay a SINGLE expression (a second sort key silently collapses the lakebase index path to a seq scan); the query vector is bound as `(:qvec)::vector` via `format_vector_literal` (repr-based), never interpolated. Process-scoped lazy `get_embedder` singleton mirrors `app.main.get_engine` |
| `errors.py` | `QueryTooBroadError` + `reraise_or_query_too_broad`: maps `psycopg.errors.QueryCanceled` (statement_timeout) to the shared error so grep and symbols raise identically without importing each other; anything else re-raises unchanged |
| `__init__.py` | Empty package marker |

## For AI Agents

### Working In This Directory
- **The compiler is the single source of truth for which files match.** grep and symbols never re-derive predicate/case logic; they compose `parse`/`resolve_case`/`compile_query`. grep owns only *which lines* match; symbols only *which definitions*.
- **`yield_per=1` on the content fetch is load-bearing** (bounds memory to ~one file), as is checking the byte cap BEFORE `.encode()` (char count is a valid lower bound on UTF-8 bytes).
- **Total failure raises, partial success flags.** A statement_timeout on the candidate query raises `QueryTooBroadError` (an empty return would be a lie); a tripped byte/row cap returns `truncated=True` + `truncation_reason`. Grep's shape flags are per-leg facts — the envelope in `app/service.py` ANDs in "the symbol leg did not answer"; do not special-case `SymbolFilter` inside grep.
- **Documented V1 caveats (do not "fix" silently):** Python `re` is not Postgres POSIX ARE (uncompilable atom → skipped + `regex_incompatible`); non-ASCII case-folding divergence is unsignalled; Python regex CPU is uncapped (catastrophic backtracking holds the GIL — real fix is an RE2 binding).
- **Semantic determinism rules:** `row_number()` ties break on `id` and the fused `ORDER BY rrf DESC, id LIMIT` tiebreak is load-bearing, but the inner leg `ORDER BY` must NEVER gain an `, id` — see `_leg_cte`'s docstring. The ANN leg's `c.embedding IS NOT NULL` guard keeps NULL embeddings from earning RRF credit.
- The branch predicate (`coalesce(r.default_branch,'HEAD') = ANY(f.branches)` / `f.branches @> ARRAY[:branch]`) must stay byte-identical to the compiler/`get_file`/0003-backfill sites.
- `re._parser` access stays a guarded `getattr` inside `_zero_width_only_atoms` — a module-scope import failing on a future CPython would take down the whole MCP server; `test_getwidth_private_api_canary` trips loudly instead.
- Timeouts are set injection-safe and transaction-local: `set_config('statement_timeout', :ms, true)` (grep/symbols) or int-coerced `SET LOCAL` (semantic) — never a session-level `SET` that leaks onto the pooled connection.

### Testing Requirements
- `make test`: `tests/unit/test_grep.py` (pure helpers, span merge, zero-width, cursor), `test_symbols_search.py`, `test_semantic.py` (SQL shape, vector literal, flag-off no-op).
- `make test-integration`: `tests/integration/test_grep.py`, `test_symbols_search.py`, `test_semantic_rrf.py` (real RRF over Postgres; lakebase operator plans are only observable on a real Lakebase branch), `test_webui_semantic.py`.

### Common Patterns
- Frozen dataclass result contracts (`GrepResult`, `FileMatches`, `LineMatch`, `SymbolResult`, `SymbolMatch`) with invariants stated in docstrings; keyword-only construction where mis-binding is a risk.
- `_Unset` sentinel for optional-kwarg mode switches (pagination), mirrored one level up in `app/service.py`.
- AST tree walks with `match` + `assert_never` tails; pure helpers kept DB-import-free and unit-testable.
- `except OperationalError as error: reraise_or_query_too_broad(error)` at every raw execution site.

## Dependencies

### Internal
- `app/query/parser.py` + `app/query/compiler.py` (the pure seams), `app/db/models.py` (`File`, `Symbol`), `app/config.py` (`Settings`), `app/embed.py` (lazy, semantic enabled-path only).

### External
`sqlalchemy` (Core, `text`, `tuple_`, `OperationalError`), `psycopg` (`errors.QueryCanceled`), stdlib `re`/`threading`; `databricks-sdk` transitively via `app.embed` (lazy).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
