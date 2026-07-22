"""Zoekt-style query string -> immutable AST (scanner + recursive-descent parser).

This module turns a user-facing zoekt search string (e.g.
``repo:acme lang:go /Foo.*Bar/ case:yes``) into a small, frozen, hashable AST that
the compiler (:mod:`app.query.compiler`) lowers to a Postgres query. It is deliberately
dependency-free (pure stdlib) so it can be imported and unit-tested without touching the
database, the Databricks SDK, or tree-sitter -- a property enforced by a subprocess
purity test.

Design: a hand-written scanner (:func:`tokenize`) produces a flat token stream in which
runs of whitespace are the AND separator (consumed here), and a recursive-descent parser
(:func:`parse`) builds an n-ary boolean tree. The AST nodes are a plain union alias with
no common base class, so the compiler gets `match`/exhaustiveness for free.

Contract / divergence notes (load-bearing for the compiler and future work):

* Filter-value interpretation contract: ``repo:``/``file:``/``sym:`` values are treated
  as regular expressions by the compiler, and ``lang:`` is normalized by the compiler
  against the ``indexer/languages.py`` ``EXT_TO_LANG`` target vocabulary
  ({python, javascript, typescript, tsx, go, java, rust}). That vocabulary is the
  documented target; it is intentionally NOT enforced here -- filter values are opaque.
* Regex bodies are stored RAW and are never ``re.compile``-d here: Postgres POSIX ARE is
  not Python ``re``, so compiling would false-reject valid patterns. Only delimiter
  structure (open ``/`` ... closing unescaped ``/``) is validated; body validity is the
  compiler's job against the real Postgres engine. Thus ``/[/`` PARSES to ``Regex("[")``.
* Lexical negation: a token-initial ``-`` (one whose next char exists, is non-whitespace,
  and is not ``)``) lexes as a :class:`TokenKind.NOT` token and negates the immediately
  following primary -- ``-foo``, ``-"x"``, ``-/re/``, ``-repo:x`` (any field), and negated
  groups ``-(a b)`` all parse to a :class:`Not` wrapper; ``NOT`` binds tighter than the
  implicit whitespace-AND. Any other ``-`` (``a - b``, trailing ``foo -``, ``(a -)``,
  interior ``foo-bar``, dashes inside field values) falls through to the bareword scan
  byte-for-byte, so only a leading ``-`` in operand position triggers negation. A stored
  ``-foo`` query now parses as negation rather than a literal substring; the escape hatch
  is quoting, ``"-foo"`` still parses as ``Substring("-foo")``. Double negation is not
  simplified (``--foo`` stays ``Not(Not(Substring("foo")))``, exact AST).
* The OR operator is accepted case-insensitively (``or``/``OR``/``Or``). The query-writing
  layer emits ``OR`` while live zoekt uses lowercase ``or``; this is a deliberate
  divergence. The literal word is still reachable via quoting (``"or"`` -> ``Substring("or")``).
* Default matching is case-INSENSITIVE, diverging from zoekt smart-case/auto. The
  ``case_sensitive: bool`` field cannot express ``auto``, so a future smart-case feature
  is a bool -> enum migration.
"""

from __future__ import annotations

import re
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
    """``lang:<lang>`` -- restrict to a language (the compiler normalizes/validates, not here)."""

    lang: str


@dataclass(frozen=True)
class SymbolFilter:
    """``sym:<name>`` -- restrict to symbol names matching ``name`` (opaque here)."""

    name: str


@dataclass(frozen=True)
class BranchFilter:
    """``branch:<value>`` -- restrict to files whose ``branches`` membership includes
    ``value`` (exact match, opaque here -- the compiler lowers it to a GIN-served ``@>``)."""

    value: str


@dataclass(frozen=True)
class CommitFilter:
    """``commit:<hash>`` -- restrict to files indexed at a git commit whose SHA starts with
    ``value`` (hex prefix, 7--40 chars, already lowercased + validated here). The compiler lowers
    it to an ``EXISTS`` against ``repo_branches`` -- resolution reads ``last_indexed_commit`` only,
    NEVER ``files.commit``."""

    value: str


@dataclass(frozen=True)
class Not:
    """Lexical negation of a single ``child`` primary (``-foo``, ``-repo:x``, ``-(a b)``).

    No double-negation simplification: ``--foo`` is ``Not(Not(Substring("foo")))``, kept
    exact. Lowered to a three-valued SQL ``NOT (...)`` by the compiler (a NULL column matches
    neither the positive nor the negated form)."""

    child: Node


@dataclass(frozen=True)
class And:
    """N-ary conjunction. Invariant: ``len(children) >= 2``."""

    children: tuple[Node, ...]


@dataclass(frozen=True)
class Or:
    """N-ary disjunction. Invariant: ``len(children) >= 2``."""

    children: tuple[Node, ...]


# A plain union alias -- no common base class -- so the compiler gets match/exhaustiveness.
Node: TypeAlias = (
    Substring
    | Regex
    | RepoFilter
    | PathFilter
    | LangFilter
    | SymbolFilter
    | BranchFilter
    | CommitFilter
    | Not
    | And
    | Or
)


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
    NOT = auto()  # a token-initial '-' in operand position (see tokenize)
    SUBSTRING = auto()
    REGEX = auto()
    REPO = auto()
    PATH = auto()
    LANG = auto()
    SYMBOL = auto()
    BRANCH = auto()
    COMMIT = auto()
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
_SUPPORTED = frozenset({"repo", "file", "lang", "sym", "case", "branch", "commit"})
_RESERVED = frozenset({"content", "r", "f", "l", "b", "c", "s"})
_FIELD_KINDS: dict[str, TokenKind] = {
    "repo": TokenKind.REPO,
    "file": TokenKind.PATH,
    "lang": TokenKind.LANG,
    "sym": TokenKind.SYMBOL,
    "branch": TokenKind.BRANCH,
    "commit": TokenKind.COMMIT,
}

# A git object name: hex only, git's own >= 7-char abbreviation minimum, full SHA-1 max of 40.
_COMMIT_HASH = re.compile(r"[0-9a-f]{7,40}")

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
    if field == "commit":
        # Case-normalize then validate: git SHAs are lowercase hex, so an upper/mixed-case input
        # is normalized (not rejected) before the hex/length check gates the token.
        normalized = value.lower()
        if not _COMMIT_HASH.fullmatch(normalized):
            raise QueryParseError(
                f"commit: expects a hex git hash of 7-40 chars, got '{value}'", start
            )
        return Token(TokenKind.COMMIT, normalized, start)
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
        # Token-initial '-' negates the following primary, but ONLY when a primary can follow:
        # the next char must exist, be non-whitespace, and not be ')'. Otherwise (EOF, `a - b`,
        # `foo -`, `(a -)`) the '-' falls through to the bareword scan and stays a literal, so
        # every pre-negation query without a leading '-' is preserved byte-for-byte.
        if c == "-" and i + 1 < n and query[i + 1] not in _WHITESPACE and query[i + 1] != ")":
            tokens.append(Token(TokenKind.NOT, "-", i))
            i += 1
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


@dataclass
class _RawNot:
    """A parsed ``NOT primary``. ``position`` is the NOT token's column, kept so ``_finalize``
    can raise at the right place when the negated operand carries no real term (``-case:yes``)."""

    child: _Raw
    position: int


_Raw: TypeAlias = Node | _RawAnd | _RawOr | _RawNot | _CaseMarker


class _Parser:
    """Recursive-descent parser over a token list. Grammar (lowest to highest):

    or_expr  := and_expr ( OR and_expr )*
    and_expr := primary ( primary )*
    primary  := NOT primary | '(' or_expr ')' | FIELD | REGEX | STRING | TERM | CASE
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
        if tok.kind == TokenKind.NOT:
            self._advance()
            # depth tracks NESTING, not a running count: NOT recurses into parse_primary and
            # decrements on the way out, so a flat `-a -b -c ...` chain (each sibling parsed and
            # unwound independently) never accumulates, while `--...--a` / `-(-(...))` does.
            self.depth += 1
            if self.depth > _MAX_DEPTH:
                raise QueryParseError("expression nesting too deep", tok.position)
            child = self.parse_primary()
            self.depth -= 1
            return _RawNot(child, tok.position)
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
        if kind == TokenKind.BRANCH:
            return BranchFilter(tok.value)
        if kind == TokenKind.COMMIT:
            return CommitFilter(tok.value)
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
    if isinstance(raw, _RawNot):
        child = _finalize(raw.child)
        if child is None:
            # The negated operand carried no real term (only case markers), e.g. `-case:yes`
            # or `-(case:yes)`: negating a query-global flag is meaningless, so raise loudly
            # rather than silently drop the NOT.
            raise QueryParseError("negation has no term to negate", raw.position)
        return Not(child)
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


def resolve_case(query: str) -> bool:
    """Return the query-global case flag (last ``case:`` wins; default False).

    A thin, stdlib-only wrapper over :func:`tokenize` + :func:`_resolve_case`. The compiler
    holds only the AST (where case is stamped on ``Substring``/``Regex`` leaves); a caller
    holding the raw query string can pass ``resolve_case(query)`` so a filter-only
    ``case:yes`` query (e.g. ``case:yes file:x``) resolves case exactly instead of falling
    back to insensitive. Additive: no node change, no :func:`parse` output change.
    """
    return _resolve_case(tokenize(query))


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
