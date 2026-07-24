"""Symbol search: ``sym:`` definition lookup over the compiler's file set.

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

* A query with no affirmative ``sym:`` atom to project returns an empty result with
  ``no_symbol_atom=True``: either no ``sym:`` atom anywhere (there is nothing to project; use
  ``search_code``/grep for content) or every ``sym:`` atom is negated (``-sym:foo`` alone --
  an exclusion has no definitions to project either). Both collect zero patterns identically;
  see :func:`_collect_symbol_patterns`. This is distinct from a ``sym:`` query that simply
  matched no symbols (``no_symbol_atom=False``, empty ``symbols``).
* Content atoms narrow which FILES are eligible but never which symbols are returned:
  ``sym:Handler foo`` returns ``Handler`` symbols from files that also contain ``foo`` -- the
  ``foo`` need not be near the definition.
* Non-``sym:`` branches of an OR are inert for symbol output: ``sym:Foo OR lang:go`` is
  equivalent to ``sym:Foo`` here, because the outer filter only keeps ``Foo``-named symbols and
  a ``Foo`` symbol only exists in files already eligible via the ``EXISTS(Foo)`` branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from sqlalchemy import Connection, Select, or_, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models import File, Symbol
from app.query.compiler import DEFAULT_ROW_LIMIT, compile_query
from app.query.parser import (
    And,
    BranchFilter,
    CommitFilter,
    LangFilter,
    Node,
    Not,
    Or,
    PathFilter,
    Regex,
    RepoFilter,
    Substring,
    SymbolFilter,
    parse,
    resolve_case,
)
from app.search.errors import reraise_or_recoverable

# Per-request DB-time bound; a cancellation surfaces as QueryTooBroadError.
DEFAULT_STATEMENT_TIMEOUT_MS = 5000


# --------------------------------------------------------------------------- contract


@dataclass(frozen=True)
class SymbolMatch:
    """One matched symbol definition. ``repo_id`` is resolved to a name by the serve layer."""

    repo_id: int
    path: str
    lang: str | None
    content_sha: str
    branches: tuple[str, ...]
    name: str
    kind: str | None
    start_line: int | None  # 1-based first line of the definition


@dataclass(frozen=True)
class SymbolResult:
    """Symbol-search result. ``truncated`` flags a row-capped page; ``no_symbol_atom`` marks a
    query with no affirmative ``sym:`` atom to project -- none at all, or every one negated
    (``-sym:foo`` alone) -- empty by construction either way, not by absence of matches."""

    symbols: tuple[SymbolMatch, ...]  # in (repo_id, path, content_sha, start_line, name, id) order
    truncated: bool  # candidate-file cap OR symbol-row cap tripped
    truncation_reason: str | None  # "row_cap" | None
    no_symbol_atom: bool


# ----------------------------------------------------------------------- pure helpers


def _collect_symbol_patterns(node: Node, out: list[str]) -> None:
    """Append every affirmative ``SymbolFilter.name`` in the AST (any And/Or nesting) to ``out``.

    All other leaves (substring/regex/repo/path/lang) contribute nothing. A :class:`Not` subtree
    is skipped (never recursed into): a ``-sym:foo`` exclusion has no definitions to project, so
    a `-sym:foo`-only query collects zero patterns -> ``no_symbol_atom=True``. Pure -- no DB
    import.
    """
    match node:
        case SymbolFilter(name=name):
            out.append(name)
        case Not():
            return
        case And(children=children) | Or(children=children):
            for child in children:
                _collect_symbol_patterns(child, out)
        case (
            Substring()
            | Regex()
            | RepoFilter()
            | PathFilter()
            | LangFilter()
            | BranchFilter()
            | CommitFilter()
        ):
            return
        case _:
            assert_never(node)


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
    # Group/order on File.repo_id (the authoritative FK grep and the compiler both key off), not
    # the denormalized Symbol.repo_id, so the merge in the serve layer never splits one physical
    # file across two (repo_id, path) entries if those FKs ever disagree.
    return (
        select(
            File.repo_id,
            File.path,
            File.lang,
            File.content_sha,
            File.branches,
            Symbol.name,
            Symbol.kind,
            Symbol.start_line,
        )
        .join(File, Symbol.file_id == File.id)
        .where(Symbol.file_id.in_(file_ids), name_filter)
        .order_by(
            File.repo_id,
            File.path,
            File.content_sha,
            Symbol.start_line,
            Symbol.name,
            Symbol.id,
        )
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
    A Postgres-invalid ``sym:`` pattern raises :class:`~app.search.errors.RegexInvalidError`.
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
        except DBAPIError as error:
            reraise_or_recoverable(error)

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
        except DBAPIError as error:
            reraise_or_recoverable(error)

    symbol_capped = len(rows) >= row_limit
    truncated = file_capped or symbol_capped
    matches = tuple(
        SymbolMatch(
            repo_id=row.repo_id,
            path=row.path,
            lang=row.lang,
            content_sha=row.content_sha,
            branches=tuple(row.branches),
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
