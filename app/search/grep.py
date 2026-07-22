"""Grep search: precise per-line match extraction over compiler candidates.

The impure orchestration layer. It composes the pure seams --
:func:`app.query.parser.parse` / :func:`app.query.parser.resolve_case` /
:func:`app.query.compiler.compile_query` -- and never re-derives predicate or case logic:
the compiler stays the single source of truth for which files match; grep owns which
lines match within them.

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
  non-empty highlight span, so two query shapes the SQL predicate does match still return
  no files. Both are announced by name rather than returning a silent empty result
  indistinguishable from a true negative:

  1. A filter-only query with no content atom (e.g. ``lang:go`` alone -- there is nothing to
     highlight; file listing is a separate concern) sets ``no_content_atom``.
  2. A query whose atoms all match zero-width (e.g. ``/^/``, ``/\b/`` -- dropped as
     non-highlights, and NOT ``regex_incompatible`` since the pattern compiled fine) sets
     ``zero_width_only_atoms``.

  Both flags are guarded by ``regex_incompatible`` (see :func:`_no_content_atom` /
  :func:`_zero_width_only_atoms`): an atom that never compiled is a content atom whose
  highlighting capability is unknown, and ``regex_incompatible`` is already its signal.
  Neither flag covers the case-folding divergence described in the NOT-RE2 bullet above:
  a file the SQL predicate matched case-insensitively that yields zero Python highlights
  drops out with patterns present, non-empty, and of non-zero width -- so it is still
  entirely unsignalled. Fixing that needs a new provable signal, not one of these two.
* **Uncapped Python CPU.** The byte cap bounds memory and aggregate bytes scanned but NOT
  CPU/wall-clock: a catastrophic-backtracking ``re`` pattern on a single under-cap file runs
  unbounded, holds the GIL, and can starve the app. ``statement_timeout`` does not cover
  Python work. No guard for this ships yet; the real fix is an RE2 binding.
* **Per-file memory** relies on the indexer's per-file byte cap (``MAX_FILE_BYTES``) keeping
  any single ``content`` bounded; ``File.size`` is nullable/unpopulated here, so a ``size``
  pre-filter is intentionally NOT used.

Byte offsets are UTF-8, line-local, half-open ``[start, end)``: for a :class:`LineMatch`,
``line_text.encode("utf-8")[start:end]`` is exactly the matched bytes (file-absolute offsets
are a documented follow-up).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple, assert_never

from sqlalchemy import Connection, select, text, tuple_
from sqlalchemy.exc import OperationalError

from app.db.models import File
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
from app.search.errors import reraise_or_query_too_broad

# 8 MiB of content pulled/scanned per request (aggregate across files).
DEFAULT_MAX_CONTENT_BYTES = 8 * 1024 * 1024
# Per-request DB-time bound; a cancellation surfaces as QueryTooBroadError.
DEFAULT_STATEMENT_TIMEOUT_MS = 5000


# --------------------------------------------------------------------------- contract


class _Unset:
    """Sentinel type for ``grep_search``'s ``cursor`` param.

    Distinguishes "no ``cursor`` argument at all" (a call that omits it, selecting the
    non-pagination path) from "``cursor`` explicitly supplied" (pagination mode, active even
    when the value is ``None`` for page 1). ``app/service.py`` mirrors this sentinel for
    ``search_code_payload``'s own ``cursor`` param and threads a decoded :class:`FileCursor`
    down to this one.
    """

    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET = _Unset()


class FileCursor(NamedTuple):
    """The ``(repo_id, path, content_sha)`` of the last candidate consumed on a page.

    This exactly matches the compiler's unique ``(repo_id, path, content_sha)`` ordering.
    ``content_sha`` is load-bearing after multi-branch content dedup: one path can have more
    than one indexed content version. The cursor tracks candidates consumed rather than emitted,
    so files with no Python-side highlights cannot stall or repeat a later page.
    """

    repo_id: int
    path: str
    content_sha: str


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
    content_sha: str
    branches: tuple[str, ...]
    line_matches: tuple[LineMatch, ...]  # non-empty


@dataclass(frozen=True)
class GrepResult:
    """A grep result. ``truncated`` (with ``truncation_reason``) flags a partial result;
    a total failure raises :class:`QueryTooBroadError` instead of returning.

    ``no_content_atom`` and ``zero_width_only_atoms`` are raw structural facts about this leg
    only. grep reports; it does not know whether a second leg answered the query -- a
    ``sym:foo`` query is genuinely filter-only here and is answered by
    :func:`app.search.symbols.symbol_search` there, so ``no_content_atom`` is ``True`` for it
    and the envelope (``app/main.py``) is what ANDs in "the symbol leg did not answer".
    Special-casing ``SymbolFilter`` here would make grep lie to a direct caller that runs no
    symbol leg.

    Invariants:

    * Mutually exclusive by construction: ``no_content_atom`` requires ``not patterns`` and
      ``zero_width_only_atoms`` requires ``bool(patterns)``, so it is never both. This also
      holds at the envelope layer, which only ever clears flags.
    * ``zero_width_only_atoms=True`` implies ``files`` is empty at this layer: every span is
      dropped by the ``m.end() > m.start()`` check in :func:`extract_line_matches`, so no
      :class:`FileMatches` is ever built. The scope matters -- at the envelope layer the same
      field can sit beside a non-empty ``files`` (``sym:Handler /^/`` folds symbol matches in),
      and it holds there only because the envelope suppresses the flag in exactly that case.
    """

    files: tuple[FileMatches, ...]  # in (repo_id, path, content_sha) order
    truncated: bool  # byte cap OR (row cap tripped AND no cursor kwarg was supplied)
    truncation_reason: str | None  # "byte_cap" | "row_cap" | None
    regex_incompatible: bool  # some Regex atom failed Python re.compile
    no_content_atom: bool  # no content atom at all (filter-only query), nothing compiled away
    zero_width_only_atoms: bool  # content atoms present, every one provably zero-width
    next_cursor: FileCursor | None  # last candidate consumed; None when this page exhausts them

    # Pagination-mode note: when ``grep_search`` is called without a ``cursor`` kwarg, a
    # row-capped result sets ``truncated=True``/``truncation_reason="row_cap"`` -- ``next_cursor``
    # is computed regardless but is meaningless to a caller that never asked for it. When
    # ``cursor`` is supplied (even ``None``, i.e. page 1), a plain row-cap fill sets
    # ``truncated=False`` + a non-null ``next_cursor`` instead (there is a next page, not an
    # error); a byte-cap trip still sets ``truncated=True`` in both modes (content was genuinely
    # dropped mid-page) and may coexist with a non-null ``next_cursor`` resuming after the last
    # row actually consumed.


# ----------------------------------------------------------------------- pure helpers


def _collect_matchers(node: Node, flags: int, patterns: list[re.Pattern[str]]) -> bool:
    """Append every AFFIRMATIVE Substring/Regex leaf's compiled pattern to ``patterns``.

    Returns True if any (affirmative) Regex leaf failed Python ``re.compile`` (NOT-RE2
    degradation). Filters (repo/path/lang/sym) contribute no patterns.

    A :class:`Not` subtree contributes NOTHING and is NOT recursed into: grep highlights only
    POSITIVE matches, and a ``-foo`` exclusion is a SQL-only predicate never rendered as a
    highlight. This also means a purely-negated broken regex (``-/[/``) does NOT set
    ``regex_incompatible`` -- the Python-side ``re.compile`` there exists only to build highlight
    patterns, and a negated atom builds none, so there is nothing whose highlighting capability
    was lost. (The SQL predicate still compiles the pattern server-side via Postgres POSIX ARE
    regardless of polarity, so exclusion correctness is unaffected.)
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
        case Not():
            return False
        case And(children=children) | Or(children=children):
            incompatible = False
            for child in children:
                incompatible = _collect_matchers(child, flags, patterns) or incompatible
            return incompatible
        case (
            RepoFilter()
            | PathFilter()
            | LangFilter()
            | SymbolFilter()
            | BranchFilter()
            | CommitFilter()
        ):
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


def _no_content_atom(patterns: Sequence[re.Pattern[str]], regex_incompatible: bool) -> bool:
    """True when the query carries no content atom at all -- a filter-only query.

    ``regex_incompatible`` is a REQUIRED conjunct, not a refinement: ``_collect_matchers``
    swallows ``re.error`` and appends nothing, so an uncompilable regex such as ``/[/`` also
    yields ``patterns == []`` despite being a content atom. That query is not filter-only --
    it *has* an atom the SQL predicate honoured and Python could not -- and its signal is
    ``regex_incompatible``, which is already set. Without the conjunct the two conditions
    would be reported as the same thing.
    """
    return not patterns and not regex_incompatible


def _zero_width_only_atoms(patterns: Sequence[re.Pattern[str]], regex_incompatible: bool) -> bool:
    """True when every content atom provably matches zero-width, so nothing can highlight.

    ``re._parser.parse(src).getwidth()`` returns ``(min_width, max_width)``; a ``max_width``
    of 0 proves the atom can never produce a non-empty span, so the ``m.end() > m.start()``
    check in :func:`extract_line_matches` always drops it. ``^``, ``$``, ``\\b`` and lookarounds
    all report ``(0, 0)``.

    Sound (given the guards) but incomplete: ``a*`` reports a huge ``max_width`` and is
    correctly not flagged -- it genuinely can highlight, so a flag would be wrong.
    (``re.compile('a*').finditer('bar')`` yields the non-empty span ``(1, 2)``; see
    ``test_zero_width_regex_matches_are_dropped``.) The residual gap is corpus-dependent and not
    statically decidable: ``a*`` over a corpus containing no ``a`` returns zero files unflagged.
    The error direction is false-negatives -- silence -- only, which is what makes the
    private-API dependency below acceptable.

    ``regex_incompatible`` is a required conjunct, mirroring :func:`_no_content_atom`. For
    ``/^/ /[/`` the ``[`` atom never compiled, so it is absent from ``patterns`` and its
    highlighting capability is unknown, not proven zero-width. Without the conjunct this would
    claim a proof it does not have.

    Empty terms are reachable and correctly flagged: ``parse('""')`` yields
    ``Substring(value='')`` and ``parse('//')`` yields ``Regex(pattern='')``, both compiling to
    a width-``(0, 0)`` pattern whose ``finditer`` yields only zero-width hits -- all dropped.
    ``True`` is the right answer for them; no guard is needed.

    The private-API access site is load-bearing. ``re._parser`` is a private CPython module and
    is reached here, inside the guarded body, never as a module-scope import beside ``import
    re``. A module-scope import that fails on a future CPython would take down this module,
    hence ``app/main.py``, hence the whole MCP server -- corruption instead of containment.
    Reached this way, the ``except Exception`` degrades the flag to ``False``, and
    ``test_getwidth_private_api_canary`` fails loudly in CI at upgrade so the degradation is
    never silent.
    """
    if not patterns or regex_incompatible:
        return False
    try:
        # getattr, not `import re._parser`: the access stays inside this guarded body.
        parser = getattr(re, "_parser")
        return all(parser.parse(pattern.pattern).getwidth()[1] == 0 for pattern in patterns)
    except Exception:
        return False


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


def grep_search(
    conn: Connection,
    query: str,
    *,
    row_limit: int = DEFAULT_ROW_LIMIT,
    max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
    cursor: FileCursor | None | _Unset = _UNSET,
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

    ``cursor`` activates pagination mode when supplied at all, including ``None`` (page 1): a
    plain row-cap fill then reports ``truncated=False`` plus a non-null
    ``GrepResult.next_cursor`` instead of ``truncated=True`` -- there is a next page, not an
    error. Omitting the kwarg entirely selects the non-pagination path: a row-capped result
    still sets ``truncated=True``/``"row_cap"``. A non-``None`` cursor also adds
    ``WHERE (files.repo_id, files.path) > (:r, :p)`` (row-value comparison) onto the compiler's
    own ``ORDER BY (repo_id, path)``, so page 1 (``cursor=None``) issues the exact query this
    function issues with no cursor.
    """
    pagination_mode = not isinstance(cursor, _Unset)
    resume: FileCursor | None = None if isinstance(cursor, _Unset) else cursor

    node = parse(query)
    case_sensitive = resolve_case(query)
    patterns, regex_incompatible = _build_matchers(node, case_sensitive)
    no_content_atom = _no_content_atom(patterns, regex_incompatible)
    zero_width_only_atoms = _zero_width_only_atoms(patterns, regex_incompatible)
    stmt = compile_query(node, limit=row_limit, case_sensitive=case_sensitive)
    if resume is not None:
        stmt = stmt.where(
            tuple_(File.repo_id, File.path, File.content_sha)
            > (resume.repo_id, resume.path, resume.content_sha)
        )

    files: list[FileMatches] = []
    byte_capped = False
    last_candidate: FileCursor | None = None

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
            reraise_or_query_too_broad(error)

        # `>=` deliberately over-warns on an exact fit (row_limit files that are exactly all
        # of them still report truncated -- an accepted, conservative false-positive).
        row_capped = len(rows) >= row_limit
        ids = [row.id for row in rows]
        if not ids:
            truncated = row_capped and not pagination_mode
            # Keyword form is required, not cosmetic: a future field must not silently
            # mis-bind into `truncated`.
            return GrepResult(
                files=(),
                truncated=truncated,
                truncation_reason="row_cap" if truncated else None,
                regex_incompatible=regex_incompatible,
                no_content_atom=no_content_atom,
                zero_width_only_atoms=zero_width_only_atoms,
                next_cursor=None,
            )

        content_stmt = (
            select(
                File.id,
                File.repo_id,
                File.path,
                File.lang,
                File.content_sha,
                File.branches,
                File.content,
            )
            .where(File.id.in_(ids))
            .order_by(File.repo_id, File.path, File.content_sha)
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
                #
                # If THIS check trips on the very first row of a page, `last_candidate` is still
                # None (never advanced -- see below), so `next_cursor` comes out None too even
                # though `byte_capped` is True: a resumed page would re-fetch the same oversized
                # row and trip the exact same cap again, stalling pagination on that one file
                # forever. Unreachable in practice: the indexer's per-file ingestion cap
                # (``MAX_FILE_BYTES`` = 1_000_000, ``indexer/languages.py``) is strictly smaller
                # than every ``max_content_bytes`` default (``DEFAULT_MAX_CONTENT_BYTES`` here,
                # ``Settings.max_content_bytes`` in ``app/config.py`` -- both 8 MiB), so no
                # ingested file can ever alone exceed the cap on the first row of a page.
                if running + len(content) > max_content_bytes:
                    byte_capped = True
                    break
                running += len(content.encode("utf-8"))
                # Consumed (scanned), regardless of whether it produces a highlight below --
                # the resume key tracks candidates CONSUMED, not candidates EMITTED. See
                # FileCursor's docstring.
                last_candidate = FileCursor(row.repo_id, row.path, row.content_sha)
                line_matches = extract_line_matches(content, patterns)
                if line_matches:
                    files.append(
                        FileMatches(
                            row.repo_id,
                            row.path,
                            row.lang,
                            row.content_sha,
                            tuple(row.branches),
                            tuple(line_matches),
                        )
                    )
        except OperationalError as error:
            reraise_or_query_too_broad(error)
        finally:
            result.close()

    truncated = byte_capped or (row_capped and not pagination_mode)
    legacy_row_capped = row_capped and not pagination_mode
    reason = "byte_cap" if byte_capped else ("row_cap" if legacy_row_capped else None)
    next_cursor = last_candidate if (row_capped or byte_capped) else None
    return GrepResult(
        files=tuple(files),
        truncated=truncated,
        truncation_reason=reason,
        regex_incompatible=regex_incompatible,
        no_content_atom=no_content_atom,
        zero_width_only_atoms=zero_width_only_atoms,
        next_cursor=next_cursor,
    )
