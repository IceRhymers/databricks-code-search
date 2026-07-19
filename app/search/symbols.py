"""Symbol search: ``sym:`` definition lookup over the compiler's file set (issue #13).

The serve-side companion to :mod:`app.search.grep` for ``sym:`` queries. The compiler lowers
``sym:Name`` to a correlated ``EXISTS(SELECT symbols.id ... WHERE symbols.name ~* 'Name')``
that answers *which files contain a matching symbol* -- it projects file columns, and grep
treats ``sym:`` as a filter contributing no highlight pattern, so ``grep_search("sym:Handler")``
returns zero files. Neither path returns the **definitions** (name / kind / line). This module
does.

Design -- two queries (mirrors grep), deliberately NOT one joined query:

1. Run :func:`app.query.compiler.compile_query` on the FULL AST standalone to concrete file
   ids. The compiler is the single source of truth for *which files match the whole query*
   (repo/lang/file/content/regex predicates + the ``sym:`` EXISTS), so symbol search inherits
   its case threading, regex opacity, ``lang:`` normalization, and ``LIKE`` escaping for free.
2. Project the symbols in those files whose name matches the query's ``sym:`` atoms, bounded
   by ``LIMIT``.

Why two queries and not a single ``symbols JOIN files JOIN repos`` with the compiler predicate
as a ``WHERE``: the compiler's ``sym:`` EXISTS ranges over ``symbols`` and its ``repo:`` subquery
over ``repos``. Embedding that predicate in a query whose FROM already contains ``symbols`` /
``repos`` would let SQLAlchemy auto-correlate those inner subqueries against the *outer* rows
(turning "any symbol in this file matches" into "this projected symbol matches"), a silent
mis-scope. ``Symbol.file_id.in_([concrete ints])`` has zero correlation surface and reuses the
compiler exactly as it is already tested.

This module is pure-SQL: definitions come straight from the projection bounded by ``LIMIT`` --
no content fetch, no ``yield_per``, no Python rescan -- so NONE of grep's NOT-RE2 /
uncapped-Python-CPU caveats apply. ``sym:`` name matching uses the SAME Postgres ``~``/``~*``
operator (and the same query-global case flag) as the compiler's ``sym:`` lowering, so an
eligible file's symbols are re-matched with identical semantics.

Semantics (load-bearing, documented, tested):

* A query with NO ``sym:`` atom returns an empty result with ``no_symbol_atom=True`` (there is
  nothing to project; use ``search_code``/grep for content). This is distinct from a ``sym:``
  query that simply matched no symbols (``no_symbol_atom=False``, empty ``symbols``).
* Content atoms narrow which FILES are eligible but never which symbols are returned:
  ``sym:Handler foo`` returns ``Handler`` symbols from files that also contain ``foo`` -- the
  ``foo`` need not be near the definition.
* Non-``sym:`` branches of an OR are inert for symbol output: ``sym:Foo OR lang:go`` is
  equivalent to ``sym:Foo`` here, because the outer filter only keeps ``Foo``-named symbols and
  a ``Foo`` symbol only exists in files already eligible via the ``EXISTS(Foo)`` branch.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, Select, or_, select, text
from sqlalchemy.exc import OperationalError

from app.db.models import File, Symbol
from app.query.compiler import DEFAULT_ROW_LIMIT, compile_query
from app.query.parser import (
    And,
    LangFilter,
    Node,
    Or,
    PathFilter,
    Regex,
    RepoFilter,
    Substring,
    SymbolFilter,
    parse,
    resolve_case,
)
from app.search.errors import reraise_or_query_too_broad

# Per-request DB-time bound; a cancellation surfaces as QueryTooBroadError.
DEFAULT_STATEMENT_TIMEOUT_MS = 5000


# --------------------------------------------------------------------------- contract


@dataclass(frozen=True)
class SymbolMatch:
    """One matched symbol definition. ``repo_id`` is resolved to a name by the serve layer."""

    repo_id: int
    path: str
    lang: str | None
    name: str
    kind: str | None
    start_line: int | None  # 1-based first line of the definition


@dataclass(frozen=True)
class SymbolResult:
    """Symbol-search result. ``truncated`` flags a row-capped page; ``no_symbol_atom`` marks a
    query that carried no ``sym:`` atom (empty by construction, not by absence of matches)."""

    symbols: tuple[SymbolMatch, ...]  # in (repo_id, path, start_line, name, id) order
    truncated: bool  # candidate-file cap OR symbol-row cap tripped
    truncation_reason: str | None  # "row_cap" | None
    no_symbol_atom: bool


# ----------------------------------------------------------------------- pure helpers


def _collect_symbol_patterns(node: Node, out: list[str]) -> None:
    """Append every ``SymbolFilter.name`` in the AST (any And/Or nesting) to ``out``.

    All other leaves (substring/regex/repo/path/lang) contribute nothing. Pure -- no DB import.
    """
    match node:
        case SymbolFilter(name=name):
            out.append(name)
        case And(children=children) | Or(children=children):
            for child in children:
                _collect_symbol_patterns(child, out)
        case Substring() | Regex() | RepoFilter() | PathFilter() | LangFilter():
            return


def _build_symbol_select(
    file_ids: list[int], patterns: list[str], *, case_sensitive: bool, row_limit: int
) -> Select:
    """Project the matching symbol definitions in ``file_ids`` (pure; renderable in tests).

    ``case_sensitive`` picks ``~`` vs ``~*`` -- the SAME operator the compiler uses for ``sym:``
    (``is_comparison=True``: ``sym:`` values are POSIX regex, not literals). The ``ORDER BY``
    ends in ``Symbol.id`` because ``symbols`` has no natural uniqueness (a file may hold two
    same-named, same-line symbols), so the id tiebreak is what makes the ``LIMIT`` page stable.
    """
    op = "~" if case_sensitive else "~*"
    name_filter = or_(*[Symbol.name.op(op, is_comparison=True)(pat) for pat in patterns])
    return (
        select(
            Symbol.repo_id,
            File.path,
            File.lang,
            Symbol.name,
            Symbol.kind,
            Symbol.start_line,
        )
        .join(File, Symbol.file_id == File.id)
        .where(Symbol.file_id.in_(file_ids), name_filter)
        .order_by(Symbol.repo_id, File.path, Symbol.start_line, Symbol.name, Symbol.id)
        .limit(row_limit)
    )


# ------------------------------------------------------------------------ entry point


def symbol_search(
    conn: Connection,
    query: str,
    *,
    row_limit: int = DEFAULT_ROW_LIMIT,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> SymbolResult:
    """Run a ``sym:`` query and return the matching symbol definitions (name/kind/file/line).

    Two-step: ``compile_query`` selects the eligible file ids, then a pure projection returns the
    symbols in those files whose name matches the query's ``sym:`` atoms. Runs in one transaction
    with a per-request ``statement_timeout`` (a cancellation raises :class:`QueryTooBroadError`).
    Raises ``QueryParseError`` on a malformed query (propagated from :func:`parse`). A query with
    no ``sym:`` atom short-circuits to an empty result (``no_symbol_atom=True``) without a DB hit.
    """
    node = parse(query)
    patterns: list[str] = []
    _collect_symbol_patterns(node, patterns)
    if not patterns:
        return SymbolResult((), truncated=False, truncation_reason=None, no_symbol_atom=True)

    case_sensitive = resolve_case(query)

    with conn.begin():
        # Injection-safe, transaction-local timeout (see grep.py): set_config binds the value as
        # a parameter and is scoped to this txn, so it never leaks onto the pooled connection.
        conn.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(statement_timeout_ms)},
        )

        # Step 1: the compiler is the single source of truth for which files match the query.
        candidate = compile_query(node, limit=row_limit, case_sensitive=case_sensitive)
        try:
            file_ids = [row.id for row in conn.execute(candidate).all()]
        except OperationalError as error:
            reraise_or_query_too_broad(error)

        # `>=` conservatively over-warns on an exact fit, matching grep's row-cap semantics.
        file_capped = len(file_ids) >= row_limit
        if not file_ids:
            return SymbolResult((), truncated=False, truncation_reason=None, no_symbol_atom=False)

        # Step 2: pure projection of the matching symbols in those files.
        stmt = _build_symbol_select(
            file_ids, patterns, case_sensitive=case_sensitive, row_limit=row_limit
        )
        try:
            rows = conn.execute(stmt).all()
        except OperationalError as error:
            reraise_or_query_too_broad(error)

    symbol_capped = len(rows) >= row_limit
    truncated = file_capped or symbol_capped
    matches = tuple(
        SymbolMatch(
            repo_id=row.repo_id,
            path=row.path,
            lang=row.lang,
            name=row.name,
            kind=row.kind,
            start_line=row.start_line,
        )
        for row in rows
    )
    return SymbolResult(
        matches,
        truncated=truncated,
        truncation_reason="row_cap" if truncated else None,
        no_symbol_atom=False,
    )
