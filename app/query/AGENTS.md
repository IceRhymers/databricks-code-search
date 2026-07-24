<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# app/query

## Purpose
The pure halves of the query pipeline. `parser.py` turns a zoekt-style query string (e.g. `repo:acme lang:go /Foo.*Bar/ case:yes`) into a small frozen, hashable AST via a hand-written scanner plus recursive-descent parser — dependency-free stdlib only, enforced by a subprocess purity test. `compiler.py` lowers that AST into a single SQLAlchemy Core `Select` over `files` whose predicates are deliberately shaped to be served by the pg_trgm GIN indexes (`ILIKE`/`LIKE`/`~`/`~*`, never a function wrapping the indexed column). `semantic_filters.py` (filter-semantics) reuses the parser's SCANNER (not `parse()`/the AST) to split a natural-language semantic query into filter atoms + residual prose, for `app/search/semantic.py`. None of the three touch a DB connection; execution lives in `app/search/`.

## Key Files
| File | Description |
|------|-------------|
| `parser.py` | Scanner (`tokenize`) + parser (`parse`) for the seven fields `repo:` `file:` `lang:` `sym:` `branch:` `commit:` `case:`, plus `/regex/`, `"quoted"`, whitespace-AND, case-insensitive `OR`, and parens (max depth 200). AST nodes: `Substring`, `Regex`, `RepoFilter`, `PathFilter`, `LangFilter`, `SymbolFilter`, `BranchFilter`, `CommitFilter`, `Not`, `And`, `Or` — a plain `Node` union alias (no base class) so consumers get `match` exhaustiveness. A token-initial `-` (next char exists, non-whitespace, not `)`) lexes as a `NOT` token and negates the following primary (`-foo`, `-repo:x`, `-(a b)`, `--foo` = double-negate, kept exact); any other `-` stays a literal (compat break: a stored `-foo` flips to negation; quote as `"-foo"` to keep the literal). `commit:` values are lowercased and validated as 7–40-char hex here; `case:` is a query-global flag (last wins, default insensitive) exposed via `resolve_case()`. Raises `QueryParseError(message, position)` — incl. `-case:yes` (negating a termless flag) and `-or` (dangling operand) |
| `compiler.py` | `compile_query(node, *, limit, case_sensitive=None) -> Select`: projects `(id, repo_id, path, lang)` (never `content`), ordered by `(repo_id, path, content_sha)` for a deterministic LIMIT page. `sym:` → correlated `EXISTS` over `symbols`; `branch:` → GIN-served `branches @> ARRAY[:v]`; `commit:` → `EXISTS` over `repo_branches` matching `lower(last_indexed_commit) LIKE lower(:prefix) || '%'`; no `branch:`/`commit:` anywhere → implicit correlated default-branch conjunct `coalesce(repos.default_branch,'HEAD') = ANY(files.branches)` (NOT GIN-served) |
| `semantic_filters.py` | `split_semantic_query(query) -> SemanticFilters` (`repo_patterns`/`path_patterns`/`langs`/`branches` tuples + `residual` str): walks `tokenize()`'s flat token stream (not the AST) and excises each `repo:`/`file:`/`lang:`/`branch:` atom's exact source span, leaving the residual natural-language prose byte-exact (only cut-boundary whitespace collapses). `sym:`/`case:`/`commit:`/regex/`NOT` (atom `"-"`) atoms raise `UnsupportedSemanticAtomError(atom, position)`; a malformed atom (e.g. bare `repo:`) raises `QueryParseError`, unchanged, straight from `tokenize()` |
| `__init__.py` | Re-exports parser names only. The compiler is deliberately NOT re-exported, or importing `app.query.parser` would pull sqlalchemy/`app.db` and fail the purity guard; `semantic_filters` is stdlib-pure too but stays out of `__init__` by the same one-purity-boundary convention — import it directly (`from app.query.semantic_filters import ...`), as `app/search/semantic.py` does |

## For AI Agents

### Working In This Directory
- **Parser purity is a hard invariant.** `parser.py` (and this package `__init__`) must import nothing beyond stdlib; `test_parser_import_is_pure` runs `import app.query.parser` in a subprocess and fails on any sqlalchemy/SDK/tree-sitter leakage.
- **Regex bodies are stored RAW, never `re.compile`d or escaped.** Postgres POSIX ARE ≠ Python `re`; validity is the DB's problem at execution time (`/[/` parses fine). The compiler binds patterns as parameters — never interpolate. `app/search/errors.py`'s `reraise_or_recoverable` maps a Postgres-invalid pattern to a typed `RegexInvalidError` -> the `regex_invalid` payload field (issue #75), never an uncaught fault.
- **Case is query-global** (last `case:` wins) but stamped only on `Substring`/`Regex` leaves; the compiler derives it from any such leaf, and callers holding the raw query pass `resolve_case(query)` so a filter-only `case:yes file:x` still resolves exactly. `repo:` is ALWAYS case-insensitive (`~*`).
- **The `coalesce(default_branch, 'HEAD')` expression must stay byte-identical** across its four sites: this compiler, the 0003 backfill, the semantic default leg, and `get_file_payload`.
- `lang:` normalizes with `.strip().lower()` and unknown values match nothing (empty result, no error, no `indexer` import). Substring literals escape `\`, `%`, `_` (backslash first) with `escape="\\"`.
- Tree walks over `Node` must end in `assert_never(node)` so a future variant is a mypy error, not a silently wrong result (see `_has_branch_filter`, `_global_case`, `_lower`).
- Parser compatibility constraints: `case_sensitive` is a bool (smart-case would require a bool→enum migration), and `_RESERVED` fields (`content`, `r`, `f`, `l`, `b`, `c`, `s`) raise rather than silently degrade.
- **`Not` node & consumer behavior:** compiler lowers `Not` to three-valued `not_(...)` (a NULL column matches neither the positive nor the negated form — no `IS NULL OR NOT` rewrite). `_has_branch_filter` is polarity-aware: only an affirmative `branch:`/`commit:` (even number of enclosing `Not`s) opts out of the implicit default-branch conjunct, so `-branch:x foo` keeps it. Consumers: grep does not highlight negated atoms (skips `Not`, so a `-/[/` negated broken regex never flags `regex_incompatible`); symbols and `service._collect_branch_filters`/`_collect_commit_filters` skip `Not` subtrees entirely (a `-sym:foo`-only query → `no_symbol_atom=True`); `service._has_content_atom` does recurse (`commit:abc -foo` is a scoped search, not a bare lookup); `semantic_filters` rejects `NOT` as atom `"-"`. The depth guard tracks nesting, so a flat `-a -b …` chain never trips it while `--…--a` does.
- `CommitFilter` counts as a branch scope in `_has_branch_filter` — removing that would silently intersect commit-scoped queries on non-default branches to zero rows.
- **Named cross-module invariant (`semantic_filters.py` ↔ `parser.py`):** `semantic_filters.py` derives field names by inverting the parser's OWN `_FIELD_KINDS` map (never a second hand-written table) and recomputes each atom's value-span end by calling the parser's OWN `_read_field_value` — the SAME scanner function that produced the token. Any change to the parser's field scanning (a new field, a new value form, different span behavior) MUST run `tests/unit/test_semantic_filters.py` — it is not exercised by `test_query_parser.py` alone. Mirrored from the consumer side in `app/search/AGENTS.md`.

### Testing Requirements
- `make test`: `tests/unit/test_query_parser.py` (grammar, errors, purity), `tests/unit/test_query_compiler.py` (SQL rendered via `stmt.compile(dialect=postgresql.dialect())` — no DB needed), `tests/unit/test_query_corpus_parity.py`, `tests/unit/test_semantic_filters.py` (filter/residual split, adversarial span-exactness, per-atom rejection, purity).
- `make test-integration`: `tests/integration/test_query_compiler.py` (predicates against real Postgres, index usage).

### Common Patterns
- Frozen dataclass AST nodes; internal mutable `_Raw*` scaffolding collapsed by `_finalize` (drops case-only operands, flattens nested And/Or, enforces `len(children) >= 2`).
- Every AST consumer uses structural `match` with an `assert_never` tail.
- Contract/divergence notes (KD-1..KD-4, zoekt divergences) live in module docstrings and are load-bearing — update them when behavior changes.

## Dependencies

### Internal
- `parser.py`: none (stdlib only — load-bearing).
- `compiler.py`: `app/db/models.py` (`File`, `Repo`, `RepoBranch`, `Symbol`), `app/query/parser.py`.
- `semantic_filters.py`: `app/query/parser.py` only (`tokenize`, `Token`, `TokenKind`, `_FIELD_KINDS`, `_read_field_value` — stdlib-pure, no `app.db`/sqlalchemy).

### External
- `parser.py`: stdlib only (`re`, `dataclasses`, `enum`).
- `compiler.py`: `sqlalchemy` (Core `select`/`exists`/`func`, postgresql `ARRAY`/`array`).
- `semantic_filters.py`: stdlib only (`dataclasses`).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
