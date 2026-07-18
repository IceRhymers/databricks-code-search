"""Zoekt-style query string -> immutable AST (scanner + recursive-descent parser).

This module turns a user-facing zoekt search string (e.g.
``repo:acme lang:go /Foo.*Bar/ case:yes``) into a small, frozen, hashable AST that
downstream issue #9 lowers to a Postgres query. It is deliberately dependency-free
(pure stdlib) so it can be imported and unit-tested without touching the database,
the Databricks SDK, or tree-sitter -- a property enforced by a subprocess purity test.

Design: a hand-written scanner (:func:`tokenize`) produces a flat token stream in which
runs of whitespace are the AND separator (consumed here), and a recursive-descent parser
(:func:`parse`) builds an n-ary boolean tree. The AST nodes are a plain union alias with
no common base class, so #9 gets `match`/exhaustiveness for free.

Contract / divergence notes (load-bearing for #9 and future work):

* Filter-value interpretation contract: ``repo:``/``file:``/``sym:`` values are treated
  as regular expressions by #9, and ``lang:`` is normalized by #9 against the
  ``indexer/languages.py`` ``EXT_TO_LANG`` target vocabulary
  ({python, javascript, typescript, tsx, go, java, rust}). That vocabulary is the
  documented *target*; it is intentionally NOT enforced here -- filter values are opaque.
* Regex bodies are stored RAW and are never ``re.compile``-d here: Postgres POSIX ARE is
  not Python ``re``, so compiling would false-reject valid patterns. Only delimiter
  structure (open ``/`` ... closing unescaped ``/``) is validated; body validity is #9's
  job against the real Postgres engine. Thus ``/[/`` PARSES to ``Regex("[")``.
* ``-foo`` is a literal :class:`Substring` in V1. When negation ships later, stored
  ``-foo`` queries will silently flip to negation -- an accepted, documented V1 risk.
* The OR operator is accepted case-insensitively (``or``/``OR``/``Or``). Issue #8 writes
  ``OR`` while live zoekt uses lowercase ``or``; this is a deliberate divergence. The
  literal word is still reachable via quoting (``"or"`` -> ``Substring("or")``).
* Default matching is case-INSENSITIVE, diverging from zoekt smart-case/auto. The
  ``case_sensitive: bool`` field cannot express ``auto``, so a future smart-case feature
  is a bool -> enum migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TypeAlias

# --------------------------------------------------------------------------- AST


@dataclass(frozen=True)
class Substring:
    """A plain substring match term."""

    value: str
    case_sensitive: bool = False


@dataclass(frozen=True)
class Regex:
    """A ``/.../`` regular-expression term. ``pattern`` is the RAW body (never compiled)."""

    pattern: str
    case_sensitive: bool = False


@dataclass(frozen=True)
class RepoFilter:
    """``repo:<pattern>`` -- restrict to repositories matching ``pattern`` (opaque here)."""

    pattern: str


@dataclass(frozen=True)
class PathFilter:
    """``file:<pattern>`` -- restrict to file paths matching ``pattern`` (opaque here)."""

    pattern: str


@dataclass(frozen=True)
class LangFilter:
    """``lang:<lang>`` -- restrict to a language (normalized/validated by #9, not here)."""

    lang: str


@dataclass(frozen=True)
class SymbolFilter:
    """``sym:<name>`` -- restrict to symbol names matching ``name`` (opaque here)."""

    name: str


@dataclass(frozen=True)
class And:
    """N-ary conjunction. Invariant: ``len(children) >= 2``."""

    children: tuple[Node, ...]


@dataclass(frozen=True)
class Or:
    """N-ary disjunction. Invariant: ``len(children) >= 2``."""

    children: tuple[Node, ...]


# A plain union alias -- no common base class -- so #9 gets match/exhaustiveness.
Node: TypeAlias = Substring | Regex | RepoFilter | PathFilter | LangFilter | SymbolFilter | And | Or


# ------------------------------------------------------------------------- errors


class QueryParseError(ValueError):
    """Raised on any malformed query. ``position`` is a 0-based column into the source."""

    def __init__(self, message: str, position: int) -> None:
        super().__init__(message)
        self.position = position


# -------------------------------------------------------------------------- tokens


class TokenKind(Enum):
    """Kinds of lexical token produced by :func:`tokenize`."""

    LPAREN = auto()
    RPAREN = auto()
    OR = auto()
    SUBSTRING = auto()
    REGEX = auto()
    REPO = auto()
    PATH = auto()
    LANG = auto()
    SYMBOL = auto()
    CASE = auto()  # value is "yes" or "no"; a zero-real-term operand (query-global flag)


@dataclass(frozen=True)
class Token:
    """A single lexical token. ``position`` is the 0-based column where it starts."""

    kind: TokenKind
    value: str
    position: int


# ------------------------------------------------------------------------- scanner

_WHITESPACE = frozenset(" \t\n")
_BAREWORD_STOP = frozenset(" \t\n()")
_SUPPORTED = frozenset({"repo", "file", "lang", "sym", "case"})
_RESERVED = frozenset({"content", "branch", "r", "f", "l", "b", "c", "s"})
_FIELD_KINDS: dict[str, TokenKind] = {
    "repo": TokenKind.REPO,
    "file": TokenKind.PATH,
    "lang": TokenKind.LANG,
    "sym": TokenKind.SYMBOL,
}

_MAX_DEPTH = 200


def _read_regex(source: str, start: int) -> tuple[str, int]:
    """Read a ``/.../`` literal at ``source[start] == '/'``; return (raw body, end index).

    ``\\/`` collapses to a literal ``/`` in the body; every other char (incl. other
    backslash escapes) is kept verbatim. Raises on EOF before the closing ``/``.
    """
    i = start + 1
    n = len(source)
    buf: list[str] = []
    while i < n:
        ch = source[i]
        if ch == "\\" and i + 1 < n and source[i + 1] == "/":
            buf.append("/")
            i += 2
            continue
        if ch == "/":
            return "".join(buf), i + 1
        buf.append(ch)
        i += 1
    raise QueryParseError("unterminated regex literal", start)


def _read_quoted(source: str, start: int) -> tuple[str, int]:
    """Read a ``"..."`` literal at ``source[start] == '"'``; return (body, end index).

    ``\\"`` collapses to a literal ``"``. Raises on EOF before the closing ``"``.
    """
    i = start + 1
    n = len(source)
    buf: list[str] = []
    while i < n:
        ch = source[i]
        if ch == "\\" and i + 1 < n and source[i + 1] == '"':
            buf.append('"')
            i += 2
            continue
        if ch == '"':
            return "".join(buf), i + 1
        buf.append(ch)
        i += 1
    raise QueryParseError("unterminated quoted string", start)


def _read_field_value(source: str, value_start: int) -> tuple[str, int]:
    """Read a field value at ``value_start``: quoted, slash-regex, or bare-until-stop."""
    n = len(source)
    if value_start < n and source[value_start] == '"':
        return _read_quoted(source, value_start)
    if value_start < n and source[value_start] == "/":
        return _read_regex(source, value_start)
    j = value_start
    while j < n and source[j] not in _BAREWORD_STOP:
        j += 1
    return source[value_start:j], j


def _emit_field(field: str, value: str, start: int) -> Token:
    """Classify a recognized SUPPORTED/RESERVED field into a token, or raise."""
    if field in _RESERVED:
        raise QueryParseError(f"reserved field '{field}' is not supported in V1", start)
    if value == "":
        raise QueryParseError(f"empty value for field '{field}'", start)
    if field == "case":
        if value not in ("yes", "no"):
            raise QueryParseError(f"case: expects 'yes' or 'no', got '{value}'", start)
        return Token(TokenKind.CASE, value, start)
    return Token(_FIELD_KINDS[field], value, start)


def tokenize(query: str) -> list[Token]:
    """Scan ``query`` into a flat token list. Whitespace separates tokens (= AND)."""
    tokens: list[Token] = []
    i = 0
    n = len(query)
    while i < n:
        c = query[i]
        if c in _WHITESPACE:
            i += 1
            continue
        if c == "(":
            tokens.append(Token(TokenKind.LPAREN, "(", i))
            i += 1
            continue
        if c == ")":
            tokens.append(Token(TokenKind.RPAREN, ")", i))
            i += 1
            continue
        if c == "/":
            start = i
            body, i = _read_regex(query, start)
            tokens.append(Token(TokenKind.REGEX, body, start))
            continue
        if c == '"':
            start = i
            body, i = _read_quoted(query, start)
            tokens.append(Token(TokenKind.SUBSTRING, body, start))
            continue

        # Bareword. First test for a recognized field prefix: [a-z]+ ':'.
        start = i
        k = i
        while k < n and "a" <= query[k] <= "z":
            k += 1
        if k > i and k < n and query[k] == ":":
            prefix = query[i:k]
            if prefix in _SUPPORTED or prefix in _RESERVED:
                value, i = _read_field_value(query, k + 1)
                tokens.append(_emit_field(prefix, value, start))
                continue

        # Not a recognized field: read the whole raw bareword until a stop char.
        j = i
        while j < n and query[j] not in _BAREWORD_STOP:
            j += 1
        lexeme = query[i:j]
        i = j
        if lexeme.lower() == "or":
            tokens.append(Token(TokenKind.OR, lexeme, start))
        else:
            tokens.append(Token(TokenKind.SUBSTRING, lexeme, start))
    return tokens


# -------------------------------------------------------------------- parse tree

# Internal-only markers used while building the boolean tree. ``case`` tokens are
# parsed as operands (so they never trigger a dangling-OR error) but carry zero real
# terms; ``_finalize`` drops them and collapses any And/Or left with < 2 real children.


@dataclass(frozen=True)
class _CaseMarker:
    """A parsed ``case:`` operand -- carries the global flag but zero real terms."""


_CASE_MARKER = _CaseMarker()


@dataclass
class _RawAnd:
    children: list[_Raw]


@dataclass
class _RawOr:
    children: list[_Raw]


_Raw: TypeAlias = Node | _RawAnd | _RawOr | _CaseMarker


class _Parser:
    """Recursive-descent parser over a token list. Grammar (lowest to highest):

    or_expr  := and_expr ( OR and_expr )*
    and_expr := primary ( primary )*
    primary  := '(' or_expr ')' | FIELD | REGEX | STRING | TERM | CASE
    """

    def __init__(self, tokens: list[Token], case_sensitive: bool) -> None:
        self.tokens = tokens
        self.case_sensitive = case_sensitive
        self.pos = 0
        self.depth = 0
        self.eof_pos = tokens[-1].position + len(tokens[-1].value) if tokens else 0

    def _peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse_or(self) -> _Raw:
        operands: list[_Raw] = [self.parse_and()]
        while True:
            tok = self._peek()
            if tok is None or tok.kind != TokenKind.OR:
                break
            or_tok = self._advance()
            nxt = self._peek()
            if nxt is None or nxt.kind in (TokenKind.OR, TokenKind.RPAREN):
                raise QueryParseError("expected an operand after 'OR'", or_tok.position)
            operands.append(self.parse_and())
        if len(operands) == 1:
            return operands[0]
        return _RawOr(operands)

    def parse_and(self) -> _Raw:
        operands: list[_Raw] = [self.parse_primary()]
        while True:
            tok = self._peek()
            if tok is None or tok.kind in (TokenKind.OR, TokenKind.RPAREN):
                break
            operands.append(self.parse_primary())
        if len(operands) == 1:
            return operands[0]
        return _RawAnd(operands)

    def parse_primary(self) -> _Raw:
        tok = self._peek()
        if tok is None:
            raise QueryParseError("unexpected end of input", self.eof_pos)
        if tok.kind == TokenKind.LPAREN:
            self._advance()
            self.depth += 1
            if self.depth > _MAX_DEPTH:
                raise QueryParseError("expression nesting too deep", tok.position)
            nxt = self._peek()
            if nxt is not None and nxt.kind == TokenKind.RPAREN:
                raise QueryParseError("empty group", tok.position)
            inner = self.parse_or()
            close = self._peek()
            if close is None or close.kind != TokenKind.RPAREN:
                raise QueryParseError("unbalanced parenthesis", tok.position)
            self._advance()
            self.depth -= 1
            return inner
        if tok.kind == TokenKind.RPAREN:
            raise QueryParseError("unexpected ')'", tok.position)
        if tok.kind == TokenKind.OR:
            raise QueryParseError("unexpected 'OR'", tok.position)
        self._advance()
        return self._build_operand(tok)

    def _build_operand(self, tok: Token) -> Node | _CaseMarker:
        kind = tok.kind
        if kind == TokenKind.SUBSTRING:
            return Substring(tok.value, self.case_sensitive)
        if kind == TokenKind.REGEX:
            return Regex(tok.value, self.case_sensitive)
        if kind == TokenKind.REPO:
            return RepoFilter(tok.value)
        if kind == TokenKind.PATH:
            return PathFilter(tok.value)
        if kind == TokenKind.LANG:
            return LangFilter(tok.value)
        if kind == TokenKind.SYMBOL:
            return SymbolFilter(tok.value)
        # kind == TokenKind.CASE
        return _CASE_MARKER


def _flatten_and(children: list[Node]) -> tuple[Node, ...]:
    out: list[Node] = []
    for child in children:
        if isinstance(child, And):
            out.extend(child.children)
        else:
            out.append(child)
    return tuple(out)


def _flatten_or(children: list[Node]) -> tuple[Node, ...]:
    out: list[Node] = []
    for child in children:
        if isinstance(child, Or):
            out.extend(child.children)
        else:
            out.append(child)
    return tuple(out)


def _finalize(raw: _Raw) -> Node | None:
    """Collapse the raw tree: drop case-only operands; enforce And/Or len >= 2.

    Returns ``None`` when a subtree has no real terms (only case markers), which the
    caller treats as an empty query at the top level.
    """
    if isinstance(raw, _CaseMarker):
        return None
    if isinstance(raw, _RawAnd):
        kids = [k for k in (_finalize(c) for c in raw.children) if k is not None]
        if not kids:
            return None
        if len(kids) == 1:
            return kids[0]
        return And(_flatten_and(kids))
    if isinstance(raw, _RawOr):
        kids = [k for k in (_finalize(c) for c in raw.children) if k is not None]
        if not kids:
            return None
        if len(kids) == 1:
            return kids[0]
        return Or(_flatten_or(kids))
    return raw


def _resolve_case(tokens: list[Token]) -> bool:
    """Resolve the query-global case flag (last ``case:`` token wins; default False)."""
    flag = False
    for tok in tokens:
        if tok.kind == TokenKind.CASE:
            flag = tok.value == "yes"
    return flag


def parse(query: str) -> Node:
    """Parse a zoekt-style query string into an immutable AST, or raise QueryParseError."""
    tokens = tokenize(query)
    if not tokens:
        raise QueryParseError("empty query", 0)
    parser = _Parser(tokens, _resolve_case(tokens))
    raw = parser.parse_or()
    leftover = parser._peek()
    if leftover is not None:
        message = "unexpected ')'" if leftover.kind == TokenKind.RPAREN else "unexpected token"
        raise QueryParseError(message, leftover.position)
    result = _finalize(raw)
    if result is None:
        raise QueryParseError("empty query", 0)
    return result
