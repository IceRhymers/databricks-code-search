"""Filter-split seam for semantic queries: strip zoekt-style filter atoms, keep the rest.

Semantic search (``app/search/semantic.py``) takes free-text natural-language queries, not
zoekt grammar -- but it still wants the same ``repo:``/``file:``/``lang:``/``branch:`` scoping
atoms :func:`app.query.parser.parse` gives ``search_code``. This module does NOT build a second
grammar or reuse :func:`parse`'s AST: it walks the SAME flat token stream (:func:`tokenize`)
and splits it into filter atoms + residual prose, byte-exactly, by excising each filter atom's
source span from the original query string.

Why not the AST (:mod:`app.query.parser`'s ``parse`` + ``Node`` tree)? Prose containing the
word "or" parses to an ``Or`` node, so a natural-language query would either error or need its
original wording guessed back from ``Substring`` leaves; quoted values and original spacing are
unrecoverable from leaves; ``case:`` markers are dropped by ``_finalize`` before they can be
rejected loudly. The flat token stream has none of these problems: a semantic query is treated
as a flat conjunction of filters + prose, with no boolean structure.

Parser-purity rules apply here too (``app/query/AGENTS.md``): stdlib only, no sqlalchemy/SDK
imports, enforced by a subprocess purity test (mirroring ``test_parser_import_is_pure``).

Field-value semantics stay OPAQUE here, exactly as in :mod:`app.query.parser` -- ``repo:``/
``file:`` values are later matched as regular expressions and ``lang:`` is normalized
(``.strip().lower()``) by ``app/search/semantic.py``'s SQL builder, matching
``app/query/compiler.py``'s ``_lower`` byte-for-byte (parity contract). ``branch:`` values are
exact, opaque strings. This module never interprets a value -- it only extracts it.

**Cross-module invariant (named, load-bearing):** field names are recovered by inverting the
parser's own ``_FIELD_KINDS`` map (never a second hand-written table), and each atom's source
span end is recomputed with the parser's own ``_read_field_value`` -- the SAME scanner function
that produced the token -- so spans are exact by construction for bare, quoted, and
slash-regex value forms alike. Any change to the parser's field scanning (new fields, new value
forms, span behavior) must run ``tests/unit/test_semantic_filters.py`` (see
``app/query/AGENTS.md`` and ``app/search/AGENTS.md``).

Worked example 1 -- innocent absolute-path prose: ``explain /etc/nginx.conf`` errors loudly.
The token-initial ``/`` scans as a ``REGEX`` token (``parser.py``'s scanner tries ``/.../``
before falling back to a bareword), so this module raises :class:`UnsupportedSemanticAtomError`
with ``atom="regex"`` -- the same rejection a deliberate ``/foo/`` atom gets. The remedy is to
quote the term: ``explain "/etc/nginx.conf"`` scans as a single quoted ``SUBSTRING`` token and
stays prose (the residual is exactly ``explain /etc/nginx.conf``, unmangled).

Worked example 2 -- ``OR`` is prose here, not boolean structure: ``repo:a or repo:b <text>``.
Both ``repo:`` atoms extract as filters (AND-composed downstream -- typically an empty
intersection, the same as lexical ``branch:a branch:b``-style conjunction); the word ``or`` is
NOT a token this module treats specially, so it stays in the residual and embeds as ordinary
prose (making the residual non-empty, so no ``nothing_to_embed``).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.query.parser import (
    _FIELD_KINDS,
    Token,
    TokenKind,
    _read_field_value,
    tokenize,
)

__all__ = [
    "SemanticFilters",
    "UnsupportedSemanticAtomError",
    "split_semantic_query",
]

# Invert the parser's OWN field->kind map at import time -- never a second hand-written table,
# so a future field added to the parser is automatically recognized here too (or, for kinds not
# present in _FIELD_KINDS at all -- REGEX, CASE -- explicitly rejected below).
_KIND_TO_FIELD: dict[TokenKind, str] = {kind: field for field, kind in _FIELD_KINDS.items()}

# Atoms with no semantic-search meaning: rejected loudly, never silently dropped or treated as
# prose. The string values are the exact ``atom`` names surfaced in the payload (app/main.py
# maps each to a remedy).
_UNSUPPORTED_ATOMS: dict[TokenKind, str] = {
    TokenKind.SYMBOL: "sym:",
    TokenKind.COMMIT: "commit:",
    TokenKind.CASE: "case:",
    TokenKind.REGEX: "regex",
}


@dataclass(frozen=True)
class SemanticFilters:
    """The result of splitting a semantic query: extracted filter values + residual prose.

    Values are opaque tuples in source (token) order, duplicates included -- downstream
    normalization (dedup, sort, ``.strip().lower()`` for ``lang:``) is ``app/search/semantic.py``'s
    job, not this module's. ``residual`` is the original query text with every filter atom's
    source span excised: interior prose spacing is preserved byte-exactly; only the whitespace
    directly at a cut boundary is collapsed to a single separating space (or dropped, if nothing
    remains on one side of a cut). An all-filters or empty/whitespace-only query yields
    ``residual == ""``.
    """

    repo_patterns: tuple[str, ...]
    path_patterns: tuple[str, ...]
    langs: tuple[str, ...]
    branches: tuple[str, ...]
    residual: str


class UnsupportedSemanticAtomError(ValueError):
    """A semantic query contained a filter atom with no semantic-search meaning.

    ``atom`` is one of ``"sym:"``, ``"case:"``, ``"commit:"``, ``"regex"`` (matching
    :data:`_UNSUPPORTED_ATOMS`'s values); ``position`` is the 0-based column of the offending
    token in the original query string, mirroring :class:`app.query.parser.QueryParseError`.
    """

    def __init__(self, atom: str, position: int) -> None:
        super().__init__(f"'{atom}' is not supported in semantic queries")
        self.atom = atom
        self.position = position


def _excise(source: str, spans: list[tuple[int, int]]) -> str:
    """Remove each ``(start, end)`` span in ``spans`` from ``source``, returning the residual.

    Splits ``source`` into the fragments BETWEEN spans (in order), strips only the leading/
    trailing whitespace of each fragment (the whitespace sitting at a cut boundary), and joins
    the non-empty results with a single space. Interior whitespace inside a fragment -- prose
    untouched by any cut -- is never touched, since ``str.strip()`` only trims the edges.
    """
    pieces: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        pieces.append(source[cursor:start])
        cursor = end
    pieces.append(source[cursor:])
    return " ".join(stripped for piece in pieces if (stripped := piece.strip()))


def split_semantic_query(query: str) -> SemanticFilters:
    """Split ``query`` into :class:`SemanticFilters`: extracted atoms + residual prose.

    Reuses :func:`tokenize` (scan errors -- an unterminated quote/regex, an empty field value,
    an invalid ``commit:``/``case:`` value -- propagate as :class:`QueryParseError`, unchanged).
    ``REPO``/``PATH``/``LANG``/``BRANCH`` tokens become filters; ``SYMBOL``/``COMMIT``/``CASE``/
    ``REGEX`` tokens raise :class:`UnsupportedSemanticAtomError`; everything else (``SUBSTRING``,
    ``OR``, ``LPAREN``, ``RPAREN``) stays untouched in the residual -- see the module docstring's
    two worked examples for the ``or``/parens-are-prose and absolute-path/regex cases.
    """
    tokens = tokenize(query)

    repo_patterns: list[str] = []
    path_patterns: list[str] = []
    langs: list[str] = []
    branches: list[str] = []
    spans: list[tuple[int, int]] = []

    for tok in tokens:
        if tok.kind in _UNSUPPORTED_ATOMS:
            raise UnsupportedSemanticAtomError(_UNSUPPORTED_ATOMS[tok.kind], tok.position)
        field = _KIND_TO_FIELD.get(tok.kind)
        if field is None:
            continue  # SUBSTRING / OR / LPAREN / RPAREN: no span, stays prose in a gap fragment
        spans.append(_field_span(query, tok, field))
        _collect(tok, repo_patterns, path_patterns, langs, branches)

    return SemanticFilters(
        repo_patterns=tuple(repo_patterns),
        path_patterns=tuple(path_patterns),
        langs=tuple(langs),
        branches=tuple(branches),
        residual=_excise(query, spans),
    )


def _field_span(query: str, tok: Token, field: str) -> tuple[int, int]:
    """Recompute ``tok``'s exact source span: ``field:value`` (or ``"..."``/ ``/.../``).

    The colon sits at ``tok.position + len(field)`` by construction (the scanner only emits a
    field token after matching ``field + ':'``); the value's end is recomputed with the SAME
    ``_read_field_value`` that produced ``tok.value`` in the first place, so the span covers
    quoted/regex delimiters exactly, not just the bareword case.
    """
    colon = tok.position + len(field)
    _, span_end = _read_field_value(query, colon + 1)
    return (tok.position, span_end)


def _collect(
    tok: Token,
    repo_patterns: list[str],
    path_patterns: list[str],
    langs: list[str],
    branches: list[str],
) -> None:
    """Append ``tok``'s value to the matching accumulator list, by token kind."""
    if tok.kind == TokenKind.REPO:
        repo_patterns.append(tok.value)
    elif tok.kind == TokenKind.PATH:
        path_patterns.append(tok.value)
    elif tok.kind == TokenKind.LANG:
        langs.append(tok.value)
    elif tok.kind == TokenKind.BRANCH:
        branches.append(tok.value)
