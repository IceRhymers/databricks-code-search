"""Parser AST -> SQLAlchemy Core ``select`` over ``files`` (issue #9).

Lowers the immutable AST produced by :mod:`app.query.parser` into a single, pure
:class:`sqlalchemy.Select`. The compiler needs no DB connection, so unit tests render
SQL via ``stmt.compile(dialect=postgresql.dialect())``. Each of the 8 node types lowers
to a ``ColumnElement[bool]`` so ``And``/``Or`` compose with ``and_()``/``or_()``.

Trigram acceleration is the point: content/path/symbol-name predicates lower to
operators the GIN ``gin_trgm_ops`` indexes can serve (``ILIKE``/``LIKE``/``~*``/``~``),
never wrapping the indexed column in a function.

Contract / divergence notes (load-bearing):

* **Case propagation (KD-1).** The frozen parser stamps ``case_sensitive`` only on
  ``Substring``/``Regex`` -- filters carry no case flag. Because case is a query-GLOBAL
  flag (parser ``_resolve_case``, last-wins), every content/regex leaf in a tree shares
  it, so we derive it from any such leaf and thread it to ``file:``/``sym:`` lowering.
  ``case_sensitive=None`` (default) derives-from-leaf (exact for any query with a
  content/regex term; insensitive fallback for a filter-only query). A caller holding
  the raw query may pass ``resolve_case(query)`` to make the filter-only ``case:yes``
  case exact. ``repo:`` is ALWAYS case-insensitive (``~*``) -- it is not in the issue's
  case-flip list.
* **Regex is opaque (KD-2 / P5).** Regex/filter patterns bind RAW as parameters -- never
  escaped, never ``re.compile``-d. An invalid POSIX ARE surfaces as a DB execution error
  at query time, not at compile time.
* **``lang:`` normalization (KD-3).** ``File.lang == lang.strip().lower()``; unknown
  values match nothing (empty result) rather than raising. No ``indexer`` import.
* **Substring escaping (KD-4 / P5).** ``LIKE``/``ILIKE`` literals escape ``\\``, ``%``,
  ``_`` (backslash first) with ``escape="\\"``.
* **Ordering / projection.** Results project ``(id, repo_id, path, lang)`` -- NOT the
  up-to-MB nullable ``content`` (phase-3 fetches content per confirmed match). Ordered by
  ``(repo_id, path)``, unique per ``UniqueConstraint(repo_id, path)`` -> a deterministic,
  stable ``LIMIT`` page with no ``id`` tiebreak. Predicate emission order does NOT drive
  execution -- Postgres reorders ANDed predicates by its own statistics.
"""

from __future__ import annotations

from typing import assert_never

from sqlalchemy import Select, and_, exists, or_, select
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import File, Repo, Symbol
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
)

# Configurable row cap; module const per repo convention (cf. _MAX_DEPTH, MAX_FILE_BYTES).
DEFAULT_ROW_LIMIT = 200

# Search-relevant projection: id + locator columns, NOT the (nullable, up-to-MB) content.
_RESULT_COLUMNS = (File.id, File.repo_id, File.path, File.lang)


def compile_query(
    node: Node,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    case_sensitive: bool | None = None,
) -> Select:
    """Lower a parser AST into a SQLAlchemy Core ``select`` over ``files``.

    Pure: needs no DB connection. ``case_sensitive=None`` (default) derives the
    query-global case flag from any ``Substring``/``Regex`` leaf (exact for any query with
    a content/regex term; insensitive fallback for filter-only queries). Callers holding
    the raw query string may pass ``resolve_case(query)`` to make the filter-only
    ``case:yes`` case exact. Raises ``ValueError`` when ``limit`` is negative.
    """
    if limit < 0:
        raise ValueError(f"limit must be non-negative, got {limit}")
    cs = _global_case(node) if case_sensitive is None else case_sensitive
    predicate = _lower(node, cs)
    return select(*_RESULT_COLUMNS).where(predicate).order_by(File.repo_id, File.path).limit(limit)


def _like_escape(value: str) -> str:
    """Escape a user literal for ``LIKE``/``ILIKE`` (backslash first, then ``%``/``_``)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _global_case(node: Node) -> bool:
    """Derive the query-global case flag: True iff any Substring/Regex leaf is stamped."""
    match node:
        case Substring(case_sensitive=cs) | Regex(case_sensitive=cs):
            return cs
        case And(children=children) | Or(children=children):
            return any(_global_case(child) for child in children)
        case RepoFilter() | PathFilter() | LangFilter() | SymbolFilter():
            return False
        case _:
            assert_never(node)


def _lower(node: Node, cs: bool) -> ColumnElement[bool]:
    """Lower a single AST node to a boolean SQL predicate. ``cs`` = global case flag."""
    match node:
        case Substring(value=value, case_sensitive=node_cs):
            pattern = f"%{_like_escape(value)}%"
            if node_cs:
                return File.content.like(pattern, escape="\\")
            return File.content.ilike(pattern, escape="\\")
        case Regex(pattern=pattern, case_sensitive=node_cs):
            op = "~" if node_cs else "~*"
            return File.content.op(op, is_comparison=True)(pattern)
        case RepoFilter(pattern=pattern):
            # repo is ALWAYS case-insensitive (not in the issue's case-flip list).
            return File.repo_id.in_(
                select(Repo.id).where(Repo.name.op("~*", is_comparison=True)(pattern))
            )
        case PathFilter(pattern=pattern):
            op = "~" if cs else "~*"
            return File.path.op(op, is_comparison=True)(pattern)
        case LangFilter(lang=lang):
            return File.lang == lang.strip().lower()
        case SymbolFilter(name=name):
            op = "~" if cs else "~*"
            return exists(
                select(Symbol.id).where(
                    and_(
                        Symbol.file_id == File.id,
                        Symbol.name.op(op, is_comparison=True)(name),
                    )
                )
            )
        case And(children=children):
            return and_(*[_lower(child, cs) for child in children])
        case Or(children=children):
            return or_(*[_lower(child, cs) for child in children])
        case _:
            assert_never(node)
