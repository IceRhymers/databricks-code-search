"""Zoekt-style query parsing: query string -> immutable, hashable AST.

See :mod:`app.query.parser` for the scanner, grammar, and contract/divergence notes.
"""

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
    "tokenize",
]
