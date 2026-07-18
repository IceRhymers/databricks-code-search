"""Zoekt-style query parsing: query string -> immutable, hashable AST.

See :mod:`app.query.parser` for the scanner, grammar, and contract/divergence notes.
"""

# NOTE: ``app.query.compiler`` (issue #9) is intentionally NOT re-exported here. The
# parser-purity test (``test_parser_import_is_pure``) does ``import app.query.parser``,
# which executes this package ``__init__``; re-exporting the compiler would pull
# ``sqlalchemy``/``app.db`` into that import chain and fail the purity guard. ``resolve_case``
# below is stdlib-only (parser-pure), so it is safe to re-export.
from app.query.parser import (
    And,
    LangFilter,
    Node,
    Or,
    PathFilter,
    QueryParseError,
    Regex,
    RepoFilter,
    Substring,
    SymbolFilter,
    Token,
    TokenKind,
    parse,
    resolve_case,
    tokenize,
)

__all__ = [
    "And",
    "LangFilter",
    "Node",
    "Or",
    "PathFilter",
    "QueryParseError",
    "Regex",
    "RepoFilter",
    "Substring",
    "SymbolFilter",
    "Token",
    "TokenKind",
    "parse",
    "resolve_case",
    "tokenize",
]
