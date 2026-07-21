<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# app/query

## Purpose
The two pure halves of the lexical query pipeline. `parser.py` turns a zoekt-style query string (e.g. `repo:acme lang:go /Foo.*Bar/ case:yes`) into a small frozen, hashable AST via a hand-written scanner plus recursive-descent parser — dependency-free stdlib only, enforced by a subprocess purity test. `compiler.py` lowers that AST into a single SQLAlchemy Core `Select` over `files` whose predicates are deliberately shaped to be served by the pg_trgm GIN indexes (`ILIKE`/`LIKE`/`~`/`~*`, never a function wrapping the indexed column). Neither module touches a DB connection; execution lives in `app/search/`.

## Key Files
| File | Description |
|------|-------------|
| `parser.py` | Scanner (`tokenize`) + parser (`parse`) for the seven fields `repo:` `file:` `lang:` `sym:` `branch:` `commit:` `case:`, plus `/regex/`, `"quoted"`, whitespace-AND, case-insensitive `OR`, and parens (max depth 200). AST nodes: `Substring`, `Regex`, `RepoFilter`, `PathFilter`, `LangFilter`, `SymbolFilter`, `BranchFilter`, `CommitFilter`, `And`, `Or` — a plain `Node` union alias (no base class) so consumers get `match` exhaustiveness. `commit:` values are lowercased and validated as 7–40-char hex here; `case:` is a query-global flag (last wins, default insensitive) exposed via `resolve_case()`. Raises `QueryParseError(message, position)` |
| `compiler.py` | `compile_query(node, *, limit, case_sensitive=None) -> Select`: projects `(id, repo_id, path, lang)` (never `content`), ordered by `(repo_id, path, content_sha)` for a deterministic LIMIT page. `sym:` → correlated `EXISTS` over `symbols`; `branch:` → GIN-served `branches @> ARRAY[:v]`; `commit:` → `EXISTS` over `repo_branches` matching `lower(last_indexed_commit) LIKE lower(:prefix) || '%'`; no `branch:`/`commit:` anywhere → implicit correlated default-branch conjunct `coalesce(repos.default_branch,'HEAD') = ANY(files.branches)` (NOT GIN-served) |
| `__init__.py` | Re-exports parser names only — the compiler is deliberately NOT re-exported, or importing `app.query.parser` would pull sqlalchemy/`app.db` and fail the purity guard |

## For AI Agents

### Working In This Directory
- **Parser purity is a hard invariant.** `parser.py` (and this package `__init__`) must import nothing beyond stdlib; `test_parser_import_is_pure` runs `import app.query.parser` in a subprocess and fails on any sqlalchemy/SDK/tree-sitter leakage.
- **Regex bodies are stored RAW, never `re.compile`d or escaped.** Postgres POSIX ARE ≠ Python `re`; validity is the DB's problem at execution time (`/[/` parses fine). The compiler binds patterns as parameters — never interpolate.
- **Case is query-global** (last `case:` wins) but stamped only on `Substring`/`Regex` leaves; the compiler derives it from any such leaf, and callers holding the raw query pass `resolve_case(query)` so a filter-only `case:yes file:x` still resolves exactly. `repo:` is ALWAYS case-insensitive (`~*`).
- **The `coalesce(default_branch, 'HEAD')` expression must stay byte-identical** across its four sites: this compiler, the 0003 backfill, the semantic default leg, and `get_file_payload`.
- `lang:` normalizes with `.strip().lower()` and unknown values match nothing (empty result, no error, no `indexer` import). Substring literals escape `\`, `%`, `_` (backslash first) with `escape="\\"`.
- Tree walks over `Node` must end in `assert_never(node)` so a future variant is a mypy error, not a silently wrong result (see `_has_branch_filter`, `_global_case`, `_lower`).
- V1 landmines documented in the parser docstring: `-foo` is a literal substring (negation not shipped), `case_sensitive` is a bool (smart-case would be a bool→enum migration), `_RESERVED` fields (`content`, `r`, `f`, `l`, `b`, `c`, `s`) raise rather than silently degrade.
- `CommitFilter` counts as a branch scope in `_has_branch_filter` — removing that would silently intersect commit-scoped queries on non-default branches to zero rows.

### Testing Requirements
- `make test`: `tests/unit/test_query_parser.py` (grammar, errors, purity), `tests/unit/test_query_compiler.py` (SQL rendered via `stmt.compile(dialect=postgresql.dialect())` — no DB needed), `tests/unit/test_query_corpus_parity.py`.
- `make test-integration`: `tests/integration/test_query_compiler.py` (predicates against real Postgres, index usage).

### Common Patterns
- Frozen dataclass AST nodes; internal mutable `_Raw*` scaffolding collapsed by `_finalize` (drops case-only operands, flattens nested And/Or, enforces `len(children) >= 2`).
- Every AST consumer uses structural `match` with an `assert_never` tail.
- Contract/divergence notes (KD-1..KD-4, zoekt divergences) live in module docstrings and are load-bearing — update them when behavior changes.

## Dependencies

### Internal
- `parser.py`: none (stdlib only — load-bearing).
- `compiler.py`: `app/db/models.py` (`File`, `Repo`, `RepoBranch`, `Symbol`), `app/query/parser.py`.

### External
- `parser.py`: stdlib only (`re`, `dataclasses`, `enum`).
- `compiler.py`: `sqlalchemy` (Core `select`/`exists`/`func`, postgresql `ARRAY`/`array`).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
