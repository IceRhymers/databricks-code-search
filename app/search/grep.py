"""Grep search: precise per-line match extraction over compiler candidates (issue #10).

The impure phase-3 orchestration layer. It composes the pure seams --
:func:`app.query.parser.parse` / :func:`app.query.parser.resolve_case` /
:func:`app.query.compiler.compile_query` -- and never re-derives predicate or case logic:
the compiler stays the single source of truth for *which files match*; grep owns *which
lines match within them*.

Design (two-step, streamed):

1. Compile the AST to a trgm-accelerated candidate ``Select`` (id + locator columns, no
   content) and run it to get the ordered candidate ids.
2. Fetch ``content`` for those ids with ``.execution_options(yield_per=1)`` -- a one-row
   server-side cursor -- and rescan each file line-by-line in Python into zoekt-shaped
   :class:`LineMatch` groups. Bare ``stream_results`` keeps a *growing* buffer and does NOT
   bound memory; ``yield_per=1`` is load-bearing.

Bounds (bounded resource use is correctness, not polish):

* **DB time** is bounded by a per-request, transaction-local ``statement_timeout``. A
  cancellation raises :class:`QueryTooBroadError` (a timeout cancels the *candidate* query
  -> zero usable rows, so any returned value would be an empty result indistinguishable
  from "no matches" -- a lie; total failure raises, partial success flags).
* **App memory** is bounded to ~one file at a time (``yield_per=1``) plus a per-request
  aggregate byte cap on content pulled/scanned. A cap that trips sets ``truncated`` with a
  ``truncation_reason``; a capped result never masquerades as complete.

Caveats (load-bearing, documented, never silently wrong):

* **NOT RE2.** Python ``re`` is not Postgres POSIX ARE, and matching here is line-oriented:
  ``^``/``$`` are line anchors, ``.`` never crosses lines, and cross-line constructs (e.g.
  ``(?s)...``) do not span lines. A Postgres-valid regex that Python ``re`` rejects is
  skipped (that atom contributes no highlights) and ``regex_incompatible`` is set. The SQL
  predicate already selected the file; grep only degrades the *highlighting*. Case folding
  can also diverge: ``re.IGNORECASE`` (Python Unicode folding) and Postgres ``lower()`` do
  not agree on every non-ASCII pair (e.g. ``ß``/``SS``, Turkish dotless ``i``), so a file
  the SQL predicate matched case-insensitively may yield zero Python highlights and drop
  out. ASCII is unaffected.
* **Highlight-driven results.** A file appears only if at least one line produces a
  non-empty highlight span, so two query shapes the SQL predicate *does* match return no
  files: a filter-only query with no content atom (e.g. ``lang:go`` alone -- ``grep_search``
  extracts line matches and has nothing to highlight; file listing is a separate concern)
  and a query whose only atom matches zero-width (e.g. ``/^/``, ``/\b/`` -- dropped as
  non-highlights, not flagged ``regex_incompatible`` since the pattern compiled fine).
* **Uncapped Python CPU (V1 limitation).** The byte cap bounds memory and aggregate bytes
  scanned but NOT CPU/wall-clock: a catastrophic-backtracking ``re`` pattern on a single
  *under-cap* file runs unbounded, holds the GIL, and can starve the app.
  ``statement_timeout`` does not cover Python work. No guard ships in V1; the real fix is an
  RE2 binding (follow-up).
* **Per-file memory** relies on the indexer's per-file byte cap (issue #7 ``MAX_FILE_BYTES``)
  keeping any single ``content`` bounded; ``File.size`` is nullable/unpopulated in this
  branch, so a ``size`` pre-filter is intentionally NOT used.

Byte offsets are UTF-8, line-local, half-open ``[start, end)``: for a :class:`LineMatch`,
``line_text.encode("utf-8")[start:end]`` is exactly the matched bytes (file-absolute offsets
are a documented follow-up).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn, assert_never

import psycopg
from sqlalchemy import Connection, select, text
from sqlalchemy.exc import OperationalError

from app.db.models import File
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

# 8 MiB of content pulled/scanned per request (aggregate across files).
DEFAULT_MAX_CONTENT_BYTES = 8 * 1024 * 1024
# Per-request DB-time bound; a cancellation surfaces as QueryTooBroadError.
DEFAULT_STATEMENT_TIMEOUT_MS = 5000


# --------------------------------------------------------------------------- contract


@dataclass(frozen=True)
class LineMatch:
    """One matched line. ``byte_ranges`` are UTF-8, line-local, half-open, sorted,
    non-overlapping: ``line_text.encode("utf-8")[s:e]`` is exactly the matched bytes."""

    line_number: int  # 1-based
    line_text: str  # one trailing "\r" stripped; never contains "\n"
    byte_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class FileMatches:
    """All line matches for one file. Omitted entirely when it has zero line matches."""

    repo_id: int
    path: str
    lang: str | None
    line_matches: tuple[LineMatch, ...]  # non-empty


@dataclass(frozen=True)
class GrepResult:
    """A grep result. ``truncated`` (with ``truncation_reason``) flags a partial result;
    a total failure raises :class:`QueryTooBroadError` instead of returning."""

    files: tuple[FileMatches, ...]  # in (repo_id, path) order
    truncated: bool  # byte cap OR row cap tripped
    truncation_reason: str | None  # "byte_cap" | "row_cap" | None
    regex_incompatible: bool  # some Regex atom failed Python re.compile


class QueryTooBroadError(Exception):
    """The per-request statement_timeout cancelled the candidate/content query."""


# ----------------------------------------------------------------------- pure helpers


def _collect_matchers(node: Node, flags: int, patterns: list[re.Pattern[str]]) -> bool:
    """Append every Substring/Regex leaf's compiled pattern to ``patterns``.

    Returns True if any Regex leaf failed Python ``re.compile`` (NOT-RE2 degradation).
    Filters (repo/path/lang/sym) contribute no patterns.
    """
    match node:
        case Substring(value=value):
            patterns.append(re.compile(re.escape(value), flags))
            return False
        case Regex(pattern=pattern):
            try:
                patterns.append(re.compile(pattern, flags))
            except re.error:
                return True
            return False
        case And(children=children) | Or(children=children):
            incompatible = False
            for child in children:
                incompatible = _collect_matchers(child, flags, patterns) or incompatible
            return incompatible
        case RepoFilter() | PathFilter() | LangFilter() | SymbolFilter():
            return False
        case _:
            assert_never(node)


def _build_matchers(node: Node, case_sensitive: bool) -> tuple[list[re.Pattern[str]], bool]:
    """Collect every Substring/Regex leaf (any And/Or nesting) into compiled patterns.

    Filters contribute none. Substring -> ``re.compile(re.escape(value))``; Regex ->
    ``re.compile(pattern)`` catching ``re.error`` (skip that atom, flag incompatible).
    ``flags = re.IGNORECASE if not case_sensitive else 0``. Returns
    ``(patterns, regex_incompatible)``. Pure -- no DB import.
    """
    flags = re.IGNORECASE if not case_sensitive else 0
    patterns: list[re.Pattern[str]] = []
    regex_incompatible = _collect_matchers(node, flags, patterns)
    return patterns, regex_incompatible


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort char spans and merge overlapping/adjacent ones into a minimal set."""
    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _char_to_byte_ranges(line: str, spans: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    """Convert sorted, non-overlapping line-local char spans to UTF-8 byte spans.

    Walks the (already sorted) spans once, encoding only the gap before each span and
    the span itself -- never a per-char prefix table over the whole line. This keeps
    memory O(one transient slice) rather than O(len(line)) resident int objects, which
    matters for a single very long line (e.g. minified JS) whose length the indexer's
    per-file byte cap does not bound.
    """
    ranges: list[tuple[int, int]] = []
    char_cursor = 0
    byte_cursor = 0
    for start, end in spans:
        byte_cursor += len(line[char_cursor:start].encode("utf-8"))
        start_byte = byte_cursor
        byte_cursor += len(line[start:end].encode("utf-8"))
        char_cursor = end
        ranges.append((start_byte, byte_cursor))
    return tuple(ranges)


def extract_line_matches(content: str, patterns: Sequence[re.Pattern[str]]) -> list[LineMatch]:
    """Extract per-line matches from ``content`` for the given compiled ``patterns``.

    Splits on ``"\\n"`` (1-based line numbers) and strips one trailing ``"\\r"`` per line
    (CRLF and LF both yield clean ``line_text``). A line emits if it matches ANY pattern;
    zero-width matches (e.g. ``a*``, ``^``) are dropped; overlapping/adjacent spans from
    any atom are merged into a sorted, non-overlapping set; merged char endpoints are
    converted to UTF-8 byte offsets. Only lines with >=1 span produce a :class:`LineMatch`.
    Pure -- no DB import.
    """
    if not patterns:
        return []
    matches: list[LineMatch] = []
    for line_number, raw_line in enumerate(content.split("\n"), start=1):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        spans: list[tuple[int, int]] = []
        for pattern in patterns:
            for m in pattern.finditer(line):
                if m.end() > m.start():  # drop zero-width matches
                    spans.append((m.start(), m.end()))
        if not spans:
            continue
        byte_ranges = _char_to_byte_ranges(line, _merge_spans(spans))
        matches.append(LineMatch(line_number, line, byte_ranges))
    return matches


# ------------------------------------------------------------------------ entry point


def _reraise_or_query_too_broad(error: OperationalError) -> NoReturn:
    """Map a Postgres statement_timeout cancellation to :class:`QueryTooBroadError`."""
    if isinstance(error.orig, psycopg.errors.QueryCanceled):
        raise QueryTooBroadError(
            "the per-request statement_timeout cancelled a query (candidate or content "
            "fetch) -- the query is too broad for the time budget"
        ) from error
    raise error


def grep_search(
    conn: Connection,
    query: str,
    *,
    row_limit: int = DEFAULT_ROW_LIMIT,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> GrepResult:
    """Run a zoekt-style ``query`` and return file-grouped, per-line matches.

    Two-step: compile the AST to a trgm-accelerated candidate query, then stream the
    matched files' content one row at a time (``yield_per=1``) and rescan them in Python.
    Runs in one transaction at Postgres default READ COMMITTED, so a candidate whose content
    was concurrently rewritten to no longer match yields zero line matches and drops out
    (self-correcting; no stale false positives).

    A per-request ``statement_timeout`` bounds DB time (a cancellation raises
    :class:`QueryTooBroadError`); ``max_content_bytes`` bounds app memory / aggregate bytes
    scanned (a cap sets ``truncated`` + ``truncation_reason``). See the module docstring for
    the NOT-RE2 and uncapped-Python-CPU caveats. Raises ``QueryParseError`` on a malformed
    query (propagated from :func:`parse`).
    """
    node = parse(query)
    case_sensitive = resolve_case(query)
    patterns, regex_incompatible = _build_matchers(node, case_sensitive)
    stmt = compile_query(node, limit=row_limit, case_sensitive=case_sensitive)

    files: list[FileMatches] = []
    byte_capped = False

    with conn.begin():
        # Injection-safe, transaction-local timeout: SET LOCAL cannot bind the value as a
        # parameter, but set_config(..., is_local=true) can and is scoped to this txn.
        conn.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(statement_timeout_ms)},
        )

        try:
            rows = conn.execute(stmt).all()
        except OperationalError as error:
            _reraise_or_query_too_broad(error)

        # `>=` deliberately over-warns on an exact fit (row_limit files that are exactly all
        # of them still report truncated -- an accepted, conservative false-positive).
        row_capped = len(rows) >= row_limit
        ids = [row.id for row in rows]
        if not ids:
            return GrepResult((), row_capped, "row_cap" if row_capped else None, regex_incompatible)

        content_stmt = (
            select(File.id, File.repo_id, File.path, File.lang, File.content)
            .where(File.id.in_(ids))
            .order_by(File.repo_id, File.path)
            .execution_options(yield_per=1)  # one-row server-side cursor; NOT bare stream_results
        )
        running = 0
        result = conn.execute(content_stmt)
        try:
            for row in result:
                content = row.content or ""
                # Char count is a valid lower bound on UTF-8 byte count, so this never
                # under-counts the cap; checked BEFORE .encode()/processing so the cap is a
                # real bound (overshoot <= one file) and avoids a transient copy of a huge file.
                if running + len(content) > max_content_bytes:
                    byte_capped = True
                    break
                running += len(content.encode("utf-8"))
                line_matches = extract_line_matches(content, patterns)
                if line_matches:
                    files.append(FileMatches(row.repo_id, row.path, row.lang, tuple(line_matches)))
        except OperationalError as error:
            _reraise_or_query_too_broad(error)
        finally:
            result.close()

    truncated = byte_capped or row_capped
    reason = "byte_cap" if byte_capped else ("row_cap" if row_capped else None)
    return GrepResult(tuple(files), truncated, reason, regex_incompatible)
