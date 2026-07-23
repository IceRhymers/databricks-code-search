"""Payload builders for the search corpus, shared by the MCP app and the web UI app.

These live here rather than in ``app/main.py`` so a second Databricks App (``webui/``) can call
the same search/file/repo-listing logic in-process without importing the MCP server module
(which builds a ``FastMCP``/Starlette ASGI app as an import side effect). ``app/main.py`` keeps
``_``-prefixed aliases (``_search_code_payload = service.search_code_payload`` etc.) for its own
call sites and existing tests.

Pure-ish builders (engine + config in, dict out) so unit tests pin the exact wire shape without
the SDK. Each opens its own connection inside the caller's thread (never shares one across
threads). Shapes are pinned to zoekt parity (``tests/unit/test_main.py``).
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, assert_never

from sqlalchemy import Text, any_, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import Engine
from sqlalchemy.sql.elements import ColumnElement

from app.config import Settings
from app.db.models import File, Repo, RepoBranch
from app.query.parser import (
    And,
    BranchFilter,
    CommitFilter,
    LangFilter,
    Node,
    Not,
    Or,
    PathFilter,
    QueryParseError,
    Regex,
    RepoFilter,
    Substring,
    SymbolFilter,
    parse,
)
from app.search.errors import QueryTooBroadError
from app.search.grep import FileCursor, grep_search
from app.search.references import EdgeSite, ReferenceResult, resolve_references
from app.search.semantic import _semantic_search_payload as semantic_search_payload  # noqa: F401
from app.search.symbols import SymbolResult, symbol_search

# `semantic_search_payload` (above) is a public re-export: the webui-facing alias of the MCP
# semantic_search tool's builder, kept here so webui/main.py imports every payload builder
# from app.service only.

# --------------------------------------------------------------------- pagination cursor


class _Unset:
    """Sentinel type for the ``cursor`` param of :func:`search_code_payload`.

    Mirrors :class:`app.search.grep._Unset`: distinguishes "no ``cursor`` argument at all"
    (callers that omit it, including the MCP ``search_code`` tool -- get the envelope with no
    ``next_cursor`` key) from "``cursor`` explicitly supplied" (pagination mode, active even
    when the value is ``None`` for page 1, which the webui API always does).
    """

    def __repr__(self) -> str:
        return "<UNSET>"


_UNSET = _Unset()

_CURSOR_VERSION = 1


class CursorError(ValueError):
    """An invalid, garbled, or version-mismatched pagination cursor.

    Raised by :func:`decode_cursor` and propagated UNCAUGHT out of :func:`search_code_payload`
    -- never swallowed into a silent restart at page 1. The API layer (``webui/main.py``) maps
    it to an HTTP 400, mirroring how it already maps ``QueryParseError``.
    """


def encode_cursor(file_cursor: FileCursor) -> str:
    """Encode a :class:`FileCursor` as an opaque base64url cursor string.

    Wire format: ``{"v": 1, "r": <repo_id>, "p": <path>, "s": <content_sha>}``, compact-JSON
    then base64url WITHOUT padding (stripped ``=``; :func:`decode_cursor` re-pads before
    decoding). Opaque to
    every caller by contract -- callers only ever round-trip it back through
    :func:`decode_cursor`/``search_code_payload(cursor=...)``, never parse it themselves.
    """
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "r": file_cursor.repo_id,
            "p": file_cursor.path,
            "s": file_cursor.content_sha,
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> FileCursor:
    """Decode an opaque cursor string produced by :func:`encode_cursor`.

    Raises :class:`CursorError` on anything malformed, garbled (tampered base64/JSON), or
    version-mismatched -- never falls back to "treat as page 1", which would silently restart
    a caller's pagination instead of surfacing the problem.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as error:
        raise CursorError(f"malformed pagination cursor: {cursor!r}") from error
    if not isinstance(data, dict) or data.get("v") != _CURSOR_VERSION:
        raise CursorError(f"unsupported or missing cursor version: {cursor!r}")
    repo_id, path, content_sha = data.get("r"), data.get("p"), data.get("s")
    # bool is an int subclass; excluded explicitly so a tampered {"r": true, ...} is rejected
    # rather than silently coerced to repo_id=1.
    if (
        not isinstance(repo_id, int)
        or isinstance(repo_id, bool)
        or not isinstance(path, str)
        or not isinstance(content_sha, str)
    ):
        raise CursorError(f"malformed pagination cursor payload: {cursor!r}")
    return FileCursor(repo_id=repo_id, path=path, content_sha=content_sha)


def _query_has_symbol_atom(node: Node) -> bool:
    """Structurally determine whether ``node`` carries a ``sym:`` atom, without a DB round trip.

    Used on pagination continuation pages, where the symbol leg does not run (folding is
    page-1-only -- see :func:`search_code_payload`): mirrors exactly what
    ``not sym_result.no_symbol_atom`` would report from a live :func:`symbol_search` run (it
    short-circuits on this identical structural check, ``symbols.py``'s ``if not patterns``),
    so query-shape suppression stays consistent across every page without reusing the
    ``sym_result is None`` timeout sentinel -- that sentinel proves a different fact (a
    timed-out DB hit), which is simply false when no DB hit was attempted at all.
    """
    match node:
        case SymbolFilter():
            return True
        case And(children=children) | Or(children=children):
            return any(_query_has_symbol_atom(child) for child in children)
        case _:
            return False


def _collect_branch_filters(node: Node) -> frozenset[str]:
    """Collect every ``branch:`` value in ``node``, under any ``And``/``Or`` nesting.

    An empty ``frozenset`` means the query carries no affirmative ``branch:`` filter anywhere
    (negated subtrees are skipped -- see the ``Not`` case). Deliberately exhaustive -- unlike
    this module's own :func:`_query_has_symbol_atom` -- with an ``assert_never(node)`` tail
    rather than a catch-all ``case _: return frozenset()``, so a future :data:`Node` variant is
    a mypy error here, not a silently-empty result.
    """
    match node:
        case BranchFilter(value=v):
            return frozenset({v})
        case Not():
            # Skip negated subtrees entirely (never recurse): this set drives permalink-branch
            # selection (_select_permalink_branch) and the branch commit-metadata lookup, both of
            # which mean "branches the author affirmatively scoped to". A `-branch:x` is an
            # exclusion, not a selection -- folding x in here would wrongly make x an eligible
            # permalink branch and fire a spurious repo_branches lookup for it.
            return frozenset()
        case And(children=children) | Or(children=children):
            return frozenset().union(*(_collect_branch_filters(c) for c in children))
        case (
            Substring()
            | Regex()
            | RepoFilter()
            | PathFilter()
            | LangFilter()
            | SymbolFilter()
            | CommitFilter()
        ):
            # CommitFilter is deliberately benign here (a skip, never a raise): a commit scope is
            # its own resolution path (see _collect_commit_filters) and carries no branch: value.
            return frozenset()
        case _:
            assert_never(node)


def _collect_commit_filters(node: Node) -> frozenset[str]:
    """Collect every ``commit:`` hex prefix in ``node``, under any ``And``/``Or`` nesting.

    An empty ``frozenset`` means the query carries no ``commit:`` atom. Mirrors
    :func:`_collect_branch_filters`: exhaustive tail (``assert_never``) so a future :data:`Node`
    variant is a mypy error, not a silently-dropped prefix.
    """
    match node:
        case CommitFilter(value=v):
            return frozenset({v})
        case Not():
            # Skip negated subtrees (same rationale as _collect_branch_filters): a `-commit:x` is
            # an exclusion predicate, not a reverse-lookup scope -- collecting x here would fire
            # commit resolution and the scoped-search path for a commit the query means to EXCLUDE.
            return frozenset()
        case And(children=children) | Or(children=children):
            return frozenset().union(*(_collect_commit_filters(c) for c in children))
        case (
            Substring()
            | Regex()
            | RepoFilter()
            | PathFilter()
            | LangFilter()
            | SymbolFilter()
            | BranchFilter()
        ):
            return frozenset()
        case _:
            assert_never(node)


def _has_content_atom(node: Node) -> bool:
    """True iff ``node`` carries a content-bearing atom (``Substring``/``Regex``/``SymbolFilter``).

    This is what distinguishes a ``commit:`` query's two moods: a bare ``commit:<hash>`` (no
    content atom) is a reverse-lookup that returns the resolution only, while ``commit:<hash>
    <terms>`` is a scoped search. ``file:``/``lang:``-only queries alongside a commit atom count
    as bare (resolution only) -- they carry no content to highlight.
    """
    match node:
        case Substring() | Regex() | SymbolFilter():
            return True
        case Not(child=child):
            # A negated content atom is still content-bearing: `commit:abc -foo` is a scoped
            # search (excluding foo), not a bare reverse-lookup, so recurse into the child.
            return _has_content_atom(child)
        case And(children=children) | Or(children=children):
            return any(_has_content_atom(child) for child in children)
        case RepoFilter() | PathFilter() | LangFilter() | BranchFilter() | CommitFilter():
            return False
        case _:
            assert_never(node)


def _select_permalink_branch(
    branch_filters: frozenset[str], row_branches: tuple[str, ...]
) -> str | None:
    """Pick the one branch a search-result row's permalink should resolve to.

    Contract: ``None`` iff ``branch_filters`` is empty -- the query had no ``branch:`` filter
    anywhere -- and ``None`` is returned here and ONLY here (or if ``row_branches`` itself is
    empty, which should not occur in practice). Otherwise ``applicable =
    branch_filters.intersection(row_branches)`` (MUST be ``.intersection()`` -- ``row_branches``
    is a tuple, so ``&`` raises ``TypeError``); when non-empty, the lexicographically smallest
    member (``sorted(applicable)[0]``) is returned -- a member of ``row_branches`` so
    :func:`get_file_payload`'s ``branches @>`` membership predicate resolves back to this exact
    content-sha row. An empty intersection with filters present is reachable only via an OR arm
    that matched this row on a non-branch predicate (e.g. ``branch:x OR lang:go``); that case
    returns ``min(row_branches)``, NEVER ``None`` -- falling back to ``None`` here would send the
    caller to default-branch resolution, which is the bug to avoid.
    """
    if not branch_filters:
        return None
    applicable = sorted(branch_filters.intersection(row_branches))
    if applicable:
        return applicable[0]
    return min(row_branches) if row_branches else None


@dataclass(frozen=True)
class ResolvedCommit:
    """One (repo, branch) head that a ``commit:`` prefix resolves to.

    Sourced from ``repo_branches.last_indexed_commit`` -- the only commit truth-source, never the
    ambiguous ``files.commit``. ``index_time`` aliases the ``last_indexed_at`` column exactly as
    :func:`list_repos_payload` does; :meth:`as_payload` is the wire shape carried by the
    ``resolved`` envelope field and the UI banner.
    """

    repo: str
    branch: str
    commit: str
    index_time: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "branch": self.branch,
            "commit": self.commit,
            "index_time": self.index_time,
        }


def resolve_commit_prefix(conn: Any, prefix: str) -> list[ResolvedCommit]:
    """Resolve a ``commit:`` hex prefix to every (repo, branch) indexed at a matching head SHA.

    Reads ONLY ``repo_branches.last_indexed_commit`` (never ``files.commit``): the column is
    aliased ``index_time`` to mirror :func:`list_repos_payload`. Both sides are lowered in SQL
    (``lower(last_indexed_commit) LIKE lower(:p) || '%'``) so a mixed-case-stored SHA still
    matches; ``prefix`` is hex-only (parser-validated) so it carries no ``LIKE`` metacharacters.
    The same ``LIKE`` expression backs the compiler's ``CommitFilter`` subquery, so the resolved
    list and the scoped-search results agree by construction.
    """
    name_map = _repo_name_map(conn)
    rows = conn.execute(
        select(
            RepoBranch.repo_id,
            RepoBranch.branch,
            RepoBranch.last_indexed_commit,
            RepoBranch.last_indexed_at,
        ).where(
            func.lower(RepoBranch.last_indexed_commit).like(func.lower(literal(prefix)).concat("%"))
        )
    ).all()
    return [
        ResolvedCommit(
            repo=name_map.get(row.repo_id, str(row.repo_id)),
            branch=row.branch,
            commit=row.last_indexed_commit,
            index_time=row.last_indexed_at.isoformat() if row.last_indexed_at else None,
        )
        for row in rows
    ]


def _scope_branch(
    branch_filters: frozenset[str],
    resolved: list[ResolvedCommit] | None,
    repo: str,
    row_branches: tuple[str, ...],
) -> str | None:
    """Pick the one branch a result row's permalink/commit metadata resolves to.

    An explicit ``branch:`` filter takes precedence (delegates to
    :func:`_select_permalink_branch`). Otherwise, for a commit-scoped query, the row was
    matched via the ``repo_branches`` subquery, so it resolves to the lexicographically smallest
    branch shared by this row and this repo's resolved commit heads. ``None`` for a query that is
    neither branch- nor commit-scoped -- the pre-existing default-branch behavior.
    """
    if branch_filters:
        return _select_permalink_branch(branch_filters, row_branches)
    if resolved:
        applicable = sorted(
            {r.branch for r in resolved if r.repo == repo}.intersection(row_branches)
        )
        return applicable[0] if applicable else None
    return None


def clamp_limit(limit: int, cfg: Settings) -> int:
    """Clamp a caller-supplied ``limit``: ``<=0`` -> default; ``> max`` -> hard cap."""
    if limit <= 0:
        return cfg.row_limit
    return min(limit, cfg.max_row_limit)


def _repo_name_map(conn: Any) -> dict[int, str]:
    """Map ``repo_id -> Repo.name`` for the current corpus (grep returns ids, not names)."""
    return {row.id: row.name for row in conn.execute(select(Repo.id, Repo.name)).all()}


def _search_envelope(
    query: str,
    *,
    files: list[dict[str, Any]],
    file_count: int,
    match_count: int,
    duration_ns: int,
    truncated: bool,
    truncation_reason: str | None,
    regex_incompatible: bool,
    query_too_broad: bool,
    query_parse_error: str | None,
    no_content_atom: bool,
    zero_width_only_atoms: bool,
    next_cursor: str | None | _Unset = _UNSET,
    resolved: list[dict[str, Any]] | _Unset = _UNSET,
    commit_not_indexed: bool | _Unset = _UNSET,
) -> dict[str, Any]:
    """Build the pinned ``search_code`` envelope (zoekt fields + additive signal fields).

    ``no_content_atom`` -- the query carried no affirmative content atom to highlight, so zero
    files is a shape outcome, not a true negative. This covers two structurally different
    queries identically: a filter-only query (e.g. ``lang:go`` alone -- nothing to highlight)
    and a fully-negated query (e.g. ``-foo`` alone -- a content atom exists but excludes rather
    than highlights). Neither has anything to highlight, so one flag suffices for both; the
    echoed ``query`` field is what a caller uses to recover which one it was, not a second key
    (see :func:`app.search.grep._no_content_atom`).
    ``zero_width_only_atoms`` -- content atoms were present but every one provably matches
    zero-width (e.g. ``/^/``), so every span was dropped. Mutually exclusive by construction.

    Both are grep's per-leg fact AND-ed with "the symbol leg did not answer this query", so
    neither ever fires beside results the caller can see. That suppression is what carries
    grep's own invariants (see :class:`app.search.grep.GrepResult`) up to this layer:

        If ``zero_width_only_atoms`` survives suppression then ``sym_answers`` is False, so
        ``sym_result`` is non-None with ``no_symbol_atom=True``, so ``sym_result.symbols`` is
        empty, so ``files`` comes only from grep -- which is empty by grep's invariant. QED

    These keys are additive and permanent: agents may depend on them, so they can never be
    removed.

    ``next_cursor`` is omitted from the envelope entirely when left at its sentinel default --
    the non-pagination shape, pinned by
    ``tests/unit/test_main.py::test_envelope_keys_are_pinned_shape_plus_exactly_two``. Pass an
    explicit ``str | None`` only when ``search_code_payload`` was itself called with a
    ``cursor`` kwarg (pagination mode).

    ``resolved``/``commit_not_indexed`` are likewise omitted unless the query carried a
    ``commit:`` atom -- so a consumer never sees the keys on a query that has no commit scope.
    When present, ``resolved`` lists every (repo, branch, commit, index_time) the prefix(es)
    resolved to and ``commit_not_indexed`` is True iff that list is empty (no indexed branch at
    that commit).
    """
    envelope: dict[str, Any] = {
        "query": query,
        "file_count": file_count,
        "match_count": match_count,
        "duration_ns": duration_ns,
        "files": files,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "regex_incompatible": regex_incompatible,
        "query_too_broad": query_too_broad,
        "query_parse_error": query_parse_error,
        "no_content_atom": no_content_atom,
        "zero_width_only_atoms": zero_width_only_atoms,
    }
    if not isinstance(next_cursor, _Unset):
        envelope["next_cursor"] = next_cursor
    if not isinstance(resolved, _Unset):
        envelope["resolved"] = resolved
    if not isinstance(commit_not_indexed, _Unset):
        envelope["commit_not_indexed"] = commit_not_indexed
    return envelope


def search_code_payload(
    engine: Engine,
    cfg: Settings,
    query: str,
    limit: int,
    cursor: str | None | _Unset = _UNSET,
) -> dict[str, Any]:
    """Run grep + symbol search and shape the merged result to the zoekt parity envelope.

    Content matches (grep) and ``sym:`` definition matches (:func:`symbol_search`) are folded
    into one ``files`` -> ``matches`` list grouped by ``(repo_id, path, content_sha)``. A path
    may have more than one indexed content version, one per divergent branch group, so the
    merge key includes ``content_sha`` to keep them as distinct file entries -- each carrying
    its own ``branches`` membership array. This matches zoekt's single-tool model where
    ``sym:`` results ride the normal envelope. A symbol match carries ``symbols: [{name,
    kind}]`` and ``line`` = the definition's first line (no highlight ``text``, so
    ``grep_search("sym:X")`` -- which returns nothing highlight-driven -- is answered here).
    ``QueryParseError`` -> ``query_parse_error`` + empty files; ``QueryTooBroadError`` (either
    leg) -> ``query_too_broad`` + ``truncated``. ``repo_id`` is resolved to ``Repo.name``.
    ``byte_ranges`` are UTF-8 line-local half-open offsets (a documented divergence from
    zoekt's char ``start_col``/``end_col``).

    Query-shape suppression. grep's ``no_content_atom`` / ``zero_width_only_atoms`` are per-leg
    facts; both are ANDed here with ``not sym_answers`` so neither fires on a query the symbol
    leg answered. ``sym:Handler`` is filter-only to grep but fully answered here, and
    ``sym:Handler /^/`` is zero-width-only to grep yet returns files -- flagging either would
    contradict the results sitting beside it and train agents to ignore the signal.
    ``no_content_atom`` itself does not distinguish filter-only from fully-negated: ``lang:go``
    and ``-foo`` both set it, and both are equally suppressed here when the symbol leg answers
    (e.g. ``sym:Handler -foo``); a caller recovers which shape it was from the echoed ``query``
    field.

    ``sym_answers`` treats an unknown symbol leg (``sym_result is None``, i.e. the leg timed
    out) as answering, which is a proof rather than a precaution: ``None`` arises only from
    ``QueryTooBroadError``, which means the DB was hit, which means the ``if not patterns``
    short-circuit in ``symbols.py`` was False, which means the query had a ``sym:`` atom. The
    inverted form (``sym_result is not None and ...``) would emit a false flag on exactly that
    timed-out ``sym:`` query. Corollary: suppression can never swallow the filter-only case it
    exists to signal -- a query like ``file:.md`` has no ``sym:`` atom, so it short-circuits
    before any DB hit and can never reach ``sym_result is None``.

    Pagination mode. Omitting ``cursor`` entirely (its sentinel default) yields the
    non-pagination envelope -- no ``next_cursor`` key, and a row-capped grep result still sets
    ``truncated=True``/``"row_cap"``. Supplying ``cursor`` at all -- including ``None`` for
    page 1, which the webui API always does -- switches to pagination mode: the envelope always
    carries a ``next_cursor`` (``str | null``), and a grep row-cap fill sets ``truncated=False``
    + a non-null ``next_cursor`` instead of an error banner (mirroring
    :func:`app.search.grep.grep_search`'s own mode gating). A garbled/tampered/version-
    mismatched ``cursor`` string raises :class:`CursorError` uncaught -- never silently
    restarts at page 1.

    The symbol leg (``sym:`` definitions) folds in only on page 1 (pagination mode with
    ``cursor=None``); continuation pages (``cursor`` is a real cursor) skip it entirely, so a
    multi-page ``sym:X foo`` result never repeats a symbol. See :func:`_query_has_symbol_atom`
    for how shape-flag suppression stays correct on those skipped pages.
    """
    pagination_mode = not isinstance(cursor, _Unset)
    decoded_cursor: FileCursor | None = None
    if isinstance(cursor, str):
        decoded_cursor = decode_cursor(cursor)  # CursorError propagates uncaught -- see docstring

    # Built as kwargs (rather than always passing `cursor=`) so a call that omits the cursor
    # omits the kwarg entirely -- grep_search's own sentinel default then selects the
    # non-pagination path, mirroring the gating one level up.
    grep_kwargs: dict[str, Any] = {
        "row_limit": limit,
        "max_content_bytes": cfg.max_content_bytes,
        "statement_timeout_ms": cfg.statement_timeout_ms,
    }
    if pagination_mode:
        grep_kwargs["cursor"] = decoded_cursor

    with engine.connect() as conn:
        t0 = time.monotonic()
        # Parse up front (rather than letting grep_search be the first parse) so commit resolution
        # can gate leg execution: a bare or unresolvable `commit:` query must never fall through
        # to an unfiltered / default-branch search. QueryParseError maps to query_parse_error --
        # an invalid `commit:` hash surfaces here.
        try:
            node = parse(query)
        except QueryParseError as error:
            return _search_envelope(
                query,
                files=[],
                file_count=0,
                match_count=0,
                duration_ns=int((time.monotonic() - t0) * 1e9),
                truncated=False,
                truncation_reason=None,
                regex_incompatible=False,
                query_too_broad=False,
                query_parse_error=str(error),
                no_content_atom=False,
                zero_width_only_atoms=False,
                next_cursor=(None if pagination_mode else _UNSET),
            )

        branch_filters = _collect_branch_filters(node)
        commit_prefixes = _collect_commit_filters(node)

        # Commit resolution runs before any leg. `resolved` stays None for a non-commit query, so
        # its envelope keys are omitted; a commit query gets a list (possibly empty). Each
        # distinct prefix resolves once; results are unioned so a prefix collision or
        # same-commit-on-many-branches scopes to every (repo, branch) pair.
        resolved: list[ResolvedCommit] | None = None
        if commit_prefixes:
            resolved = []
            for prefix in sorted(commit_prefixes):
                resolved.extend(resolve_commit_prefix(conn, prefix))
            has_content = _has_content_atom(node)
            if not resolved or not has_content:
                # Two short-circuit moods, both returning the resolution + empty results:
                #   * no resolution -> commit_not_indexed True; never an unfiltered search;
                #   * bare lookup   -> resolution only, commit is indexed.
                return _search_envelope(
                    query,
                    files=[],
                    file_count=0,
                    match_count=0,
                    duration_ns=int((time.monotonic() - t0) * 1e9),
                    truncated=False,
                    truncation_reason=None,
                    regex_incompatible=False,
                    query_too_broad=False,
                    query_parse_error=None,
                    no_content_atom=not has_content,
                    zero_width_only_atoms=False,
                    next_cursor=(None if pagination_mode else _UNSET),
                    resolved=[r.as_payload() for r in resolved],
                    commit_not_indexed=not resolved,
                )

        # Commit envelope keys for the scoped-search path (resolutions exist AND a content atom):
        # `resolved` is non-empty here, so commit_not_indexed is always False.
        commit_kwargs: dict[str, Any] = {}
        if resolved is not None:
            commit_kwargs["resolved"] = [r.as_payload() for r in resolved]
            commit_kwargs["commit_not_indexed"] = False

        try:
            result = grep_search(conn, query, **grep_kwargs)
        except QueryTooBroadError:
            # The whole query is over the time budget; the symbol leg would time out too.
            return _search_envelope(
                query,
                files=[],
                file_count=0,
                match_count=0,
                duration_ns=int((time.monotonic() - t0) * 1e9),
                truncated=True,
                truncation_reason=None,
                regex_incompatible=False,
                query_too_broad=True,
                query_parse_error=None,
                no_content_atom=False,
                zero_width_only_atoms=False,
                next_cursor=(None if pagination_mode else _UNSET),
                **commit_kwargs,
            )

        # Symbol leg: sym: definitions the highlight-driven grep path cannot return. A timeout
        # here flags query_too_broad but still returns whatever grep found (partial, not a lie).
        # Note: a `sym:X foo` query runs the compiler candidate scan twice (once per leg), each
        # under its OWN statement_timeout -- so the DB-time bound is per-leg, not a single budget.
        #
        # Page-1-only: a continuation page (pagination_mode with a real decoded_cursor) skips
        # the symbol leg so folded symbols never repeat across pages.
        run_symbol_leg = not (pagination_mode and decoded_cursor is not None)
        query_too_broad = False
        sym_result: SymbolResult | None
        if run_symbol_leg:
            try:
                sym_result = symbol_search(
                    conn,
                    query,
                    row_limit=limit,
                    statement_timeout_ms=cfg.statement_timeout_ms,
                )
            except QueryTooBroadError:
                sym_result = None
                query_too_broad = True
        else:
            sym_result = None

        duration_ns = int((time.monotonic() - t0) * 1e9)

        # Resolve names for every repo present across BOTH legs, bounded by the same
        # transaction-local statement_timeout the other raw SELECTs use (each leg's own SET LOCAL
        # committed with its transaction, so this lookup would otherwise run uncapped).
        repo_ids = {fm.repo_id for fm in result.files}
        if sym_result is not None:
            repo_ids |= {sm.repo_id for sm in sym_result.symbols}
        name_map: dict[int, str] = {}
        branch_commit_rows: list[Any] = []
        if repo_ids or branch_filters:
            with conn.begin():
                conn.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}"
                )
                if repo_ids:
                    name_map = _repo_name_map(conn)
                if branch_filters:
                    # Commit metadata for explicit branch: queries: each named branch's indexed
                    # head SHA, from repo_branches (never files.commit). Commit-scoped queries
                    # need no lookup here -- `resolved` already carries their commits.
                    branch_commit_rows = list(
                        conn.execute(
                            select(Repo.name, RepoBranch.branch, RepoBranch.last_indexed_commit)
                            .join(RepoBranch, RepoBranch.repo_id == Repo.id)
                            .where(RepoBranch.branch.in_(sorted(branch_filters)))
                        ).all()
                    )

    # Merge content + symbol matches into one file list grouped by (repo_id, path, content_sha)
    # -- content_sha disambiguates divergent branch content versions of one path.
    merged: dict[tuple[int, str, str], dict[str, Any]] = {}

    def _entry(
        repo_id: int, path: str, lang: str | None, content_sha: str, branches: tuple[str, ...]
    ) -> dict[str, Any]:
        key = (repo_id, path, content_sha)
        entry = merged.get(key)
        if entry is None:
            entry = {
                "repo_id": repo_id,
                "path": path,
                "lang": lang,
                "content_sha": content_sha,
                "branches": branches,
                "matches": [],
            }
            merged[key] = entry
        return entry

    match_count = 0
    for fm in result.files:
        entry = _entry(fm.repo_id, fm.path, fm.lang, fm.content_sha, fm.branches)
        for lm in fm.line_matches:
            entry["matches"].append(
                {
                    "line": lm.line_number,
                    "text": lm.line_text,
                    "byte_ranges": [[start, end] for start, end in lm.byte_ranges],
                }
            )
        # match_count counts matched spans (byte_ranges), not lines: the golden zoekt fixture
        # reports 2 for one line carrying two ranges.
        match_count += sum(len(lm.byte_ranges) for lm in fm.line_matches)

    if sym_result is not None:
        for sm in sym_result.symbols:
            entry = _entry(sm.repo_id, sm.path, sm.lang, sm.content_sha, sm.branches)
            entry["matches"].append(
                {
                    "line": sm.start_line,
                    "text": "",  # line + name + kind only; no def-line text yet
                    "byte_ranges": [],
                    "symbols": [{"name": sm.name, "kind": sm.kind}],
                }
            )
            match_count += 1  # each symbol definition is one match (its byte_ranges is empty)

    # (repo name, branch) -> indexed head commit for result commit metadata. Commit-scoped rows
    # come from `resolved` (already fetched); explicit branch: rows from branch_commit_rows.
    commit_map: dict[tuple[str, str], str | None] = {}
    if resolved:
        for rc in resolved:
            commit_map[(rc.repo, rc.branch)] = rc.commit
    for row in branch_commit_rows:
        commit_map[(row.name, row.branch)] = row.last_indexed_commit

    files: list[dict[str, Any]] = []
    for entry in sorted(merged.values(), key=lambda e: (e["repo_id"], e["path"], e["content_sha"])):
        # Order matches within a file by line; NULL symbol lines sort last.
        entry["matches"].sort(key=lambda m: (m["line"] is None, m["line"] or 0))
        # content_sha + permalink_branch are additive per-file keys on the payload shared by
        # the MCP search_code tool and /api/search; the lexical MCP payload is additive-only.
        # permalink_branch is service-selected -- clients must never infer a file version from
        # the query.
        repo_name = name_map.get(entry["repo_id"], str(entry["repo_id"]))
        scope_branch = _scope_branch(branch_filters, resolved, repo_name, entry["branches"])
        file_entry: dict[str, Any] = {
            "repo": repo_name,
            "file": entry["path"],
            "language": entry["lang"],
            "branches": list(entry["branches"]),  # real membership, not hardcoded
            "matches": entry["matches"],
            "content_sha": entry["content_sha"],
            "permalink_branch": scope_branch,
        }
        # `commit` is additive and present only when the scope resolves to a specific branch
        # whose indexed head we know -- so a non-scoped query's file shape is unchanged.
        if scope_branch is not None:
            commit = commit_map.get((repo_name, scope_branch))
            if commit is not None:
                file_entry["commit"] = commit
        files.append(file_entry)

    if run_symbol_leg:
        # `is None or` is load-bearing: an unknown (timed-out) symbol leg counts as answering,
        # and is provably always the sym-bearing shape. See the docstring.
        sym_answers = sym_result is None or not sym_result.no_symbol_atom
    else:
        # Deliberate page-2+ skip -- NOT the timeout sentinel. `sym_result` is None here too,
        # but reusing the `is None` proof above would wrongly claim "answers" on every
        # continuation page regardless of whether the query even has a sym: atom. Determine it
        # structurally instead (see _query_has_symbol_atom).
        sym_answers = _query_has_symbol_atom(parse(query))
    no_content_atom = result.no_content_atom and not sym_answers
    zero_width_only_atoms = result.zero_width_only_atoms and not sym_answers

    sym_truncated = sym_result.truncated if sym_result is not None else False
    truncated = result.truncated or sym_truncated or query_too_broad
    truncation_reason = result.truncation_reason or (
        sym_result.truncation_reason if sym_result is not None else None
    )
    next_cursor_out: str | None | _Unset = _UNSET
    if pagination_mode:
        if run_symbol_leg and result.no_content_atom:
            # Page 1, no affirmative content atom to highlight -- either a filter-only query
            # (e.g. a `sym:` atom with no content atom alongside it) or a fully-negated one
            # (e.g. `-foo` alone): grep's `files` is ALWAYS empty here regardless of how many
            # CANDIDATE files the filter/exclusion matched (there is no content pattern to
            # highlight), but grep's own candidate scan can still hit `row_limit` and row-cap
            # when the query matches many files -- e.g. a `sym:` name shared by >= row_limit
            # files, or `-foo` excluding a rare term from a huge corpus. Left alone, that still
            # sets a non-null `next_cursor`; the symbol leg only ever folds in on page 1 too
            # (page-1-only, see above), so every continuation page would re-run the
            # same filter-only grep scan, find nothing to highlight, and hand back ANOTHER
            # non-null cursor -- an unbounded sequence of empty pages. Suppressed here instead:
            # a query with no affirmative content atom is always exactly one page, with any real
            # "there's more" signal (e.g. more matching symbols than fit) already carried by
            # `truncated`/`truncation_reason`, not `next_cursor`.
            next_cursor_out = None
        else:
            next_cursor_out = (
                encode_cursor(result.next_cursor) if result.next_cursor is not None else None
            )
    return _search_envelope(
        query,
        files=files,
        file_count=len(files),
        match_count=match_count,
        duration_ns=duration_ns,
        truncated=truncated,
        truncation_reason=truncation_reason,
        regex_incompatible=result.regex_incompatible,
        query_too_broad=query_too_broad,
        query_parse_error=None,
        no_content_atom=no_content_atom,
        zero_width_only_atoms=zero_width_only_atoms,
        next_cursor=next_cursor_out,
        **commit_kwargs,
    )


def list_repos_payload(engine: Engine, cfg: Settings) -> dict[str, Any]:
    """List indexed repos with metadata, bounded by a transaction-local statement_timeout.

    Branches are enumerated from ``repo_branches`` -- the authoritative per-branch registry --
    rather than guessed from the single ``repos.default_branch`` stamp, so a repo indexed on
    multiple branches reports all of them. ``branches`` is a flat name list, so a single-branch
    repo still renders an array of one. Per-branch detail (``last_indexed_commit``/
    ``index_time``) is carried in ``branch_details``; the top-level ``index_time``/
    ``last_indexed_commit`` mirror the repo's default-branch row (falling back to the first
    enumerated branch if the default itself was never indexed).
    """
    with engine.connect() as conn:
        with conn.begin():
            # SET LOCAL (int-coerced -> injection-safe) is transaction-scoped, so it never
            # leaks a statement_timeout onto the pooled connection (unlike a session-level SET).
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            rows = conn.execute(
                select(
                    Repo.id,
                    Repo.name,
                    Repo.default_branch,
                    RepoBranch.branch,
                    RepoBranch.last_indexed_commit,
                    RepoBranch.last_indexed_at,
                )
                .select_from(Repo)
                .outerjoin(RepoBranch, RepoBranch.repo_id == Repo.id)
                .order_by(Repo.name, RepoBranch.branch)
            ).all()

    grouped: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for row in rows:
        entry = grouped.get(row.id)
        if entry is None:
            entry = {"name": row.name, "default_branch": row.default_branch, "branch_rows": []}
            grouped[row.id] = entry
            order.append(row.id)
        if row.branch is not None:
            entry["branch_rows"].append(
                {
                    "branch": row.branch,
                    "last_indexed_commit": row.last_indexed_commit,
                    "index_time": row.last_indexed_at.isoformat() if row.last_indexed_at else None,
                }
            )

    repos: list[dict[str, Any]] = []
    for repo_id in order:
        entry = grouped[repo_id]
        branch_rows: list[dict[str, Any]] = entry["branch_rows"]
        default = entry["default_branch"] or "HEAD"
        default_row = next((b for b in branch_rows if b["branch"] == default), None)
        if default_row is None and branch_rows:
            default_row = branch_rows[0]
        repos.append(
            {
                "name": entry["name"],
                "branches": [b["branch"] for b in branch_rows] or ["HEAD"],
                "index_time": default_row["index_time"] if default_row else None,
                "default_branch": entry["default_branch"],
                "last_indexed_commit": default_row["last_indexed_commit"] if default_row else None,
                "branch_details": branch_rows,
            }
        )
    return {"repos": repos, "count": len(repos)}


def get_file_payload(
    engine: Engine, cfg: Settings, repo: str, path: str, branch: str | None = None
) -> dict[str, Any]:
    """Fetch one file's full content by (repo name, path), scoped to one content version.

    A path may have more than one indexed content version (one per divergent branch group), so
    the predicate must resolve to <=1 row per ``(repo, path)``: an explicit ``branch`` uses the
    GIN-served exact-membership operator ``branches @> ARRAY[:branch]`` (the same as the query
    compiler's ``branch:`` lowering); omitted, it falls back to the correlated
    ``coalesce(repos.default_branch, 'HEAD') = ANY(files.branches)`` -- byte-identical to the
    compiler's implicit default conjunct and the semantic default leg. A branch name is a member
    of at most one content version of a given path (a branch points at one tree), so both
    predicates keep the single-row guarantee ``scalar_one_or_none()`` relies on -- a second row
    would raise ``MultipleResultsFound``, signalling the guarantee broke rather than a bug here.
    Returns the resolved ``branch`` (the given branch, or the repo's default / ``'HEAD'``); a
    miss stays a structured ``found: False``. The resolved branch's indexed head ``commit`` is
    included from ``repo_branches`` (never ``files.commit``), ``None`` when that branch was
    never registered.
    """
    with engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            predicate: ColumnElement[bool]
            if branch is not None:
                predicate = File.branches.op("@>")(literal([branch], type_=ARRAY(Text)))
                resolved_branch = branch
            else:
                default_branch = conn.execute(
                    select(func.coalesce(Repo.default_branch, "HEAD")).where(Repo.name == repo)
                ).scalar_one_or_none()
                resolved_branch = default_branch or "HEAD"
                predicate = func.coalesce(Repo.default_branch, "HEAD") == any_(File.branches)
            content = conn.execute(
                select(File.content)
                .join(Repo, File.repo_id == Repo.id)
                .where(Repo.name == repo, File.path == path, predicate)
            ).scalar_one_or_none()
            commit = conn.execute(
                select(RepoBranch.last_indexed_commit)
                .join(Repo, RepoBranch.repo_id == Repo.id)
                .where(Repo.name == repo, RepoBranch.branch == resolved_branch)
            ).scalar_one_or_none()
    found = content is not None
    return {
        "repo": repo,
        "path": path,
        "branch": resolved_branch,
        "content": content,
        "found": found,
        "commit": commit,
    }


# ------------------------------------------------------------------- reference resolution


def _site_payload(site: EdgeSite, name_map: dict[int, str]) -> dict[str, Any]:
    """Shape one resolved :class:`~app.search.references.EdgeSite` for the wire.

    ``symbol_id`` is deliberately absent from each candidate dict (D1/D4): it is a query-time
    ranking tiebreak, never persisted, never a caller-facing identifier.
    """
    enclosing_symbol = (
        {"name": site.enclosing_name, "kind": site.enclosing_kind}
        if site.enclosing_name is not None
        else None
    )
    return {
        "repo": name_map.get(site.repo_id, str(site.repo_id)),
        "file": site.path,
        "line": site.line,
        "edge_kind": site.edge_kind,
        "target_name": site.target_name,
        "enclosing_symbol": enclosing_symbol,
        "resolution": site.resolution,
        "candidate_count": site.candidate_count,
        "candidates_truncated": site.candidates_truncated,
        "candidates": [
            {
                "repo": name_map.get(candidate.repo_id, str(candidate.repo_id)),
                "file": candidate.path,
                "line": candidate.start_line,
                "name": candidate.name,
                "kind": candidate.kind,
                "same_repo": candidate.same_repo,
                "same_file": candidate.same_file,
                "kind_match": candidate.kind_match,
            }
            for candidate in site.candidates
        ],
    }


def _reference_result_to_payload(
    result: ReferenceResult, name_map: dict[int, str]
) -> dict[str, Any]:
    """Shared envelope shape for :func:`find_references_payload` / :func:`list_imports_payload`:
    ``sites`` + ``site_count`` + a ``resolution_summary`` histogram + truncation flags."""
    resolution_summary = {"unique": 0, "ambiguous": 0, "unresolved": 0}
    for site in result.sites:
        resolution_summary[site.resolution] += 1
    return {
        "sites": [_site_payload(site, name_map) for site in result.sites],
        "site_count": len(result.sites),
        "resolution_summary": resolution_summary,
        "truncated": result.truncated,
        "truncation_reason": result.truncation_reason,
    }


def _reference_repo_name_map(conn: Any, result: ReferenceResult, cfg: Settings) -> dict[int, str]:
    """Resolve every repo id across a :class:`ReferenceResult`'s sites AND candidates to names.

    Run in a SEPARATE ``conn.begin()`` + ``SET LOCAL statement_timeout`` AFTER
    :func:`resolve_references` returns -- its own ``set_config`` committed with its
    transaction, so this lookup would otherwise run uncapped (mirrors ``search_code_payload``'s
    post-leg ``_repo_name_map`` call).
    """
    repo_ids = {site.repo_id for site in result.sites}
    repo_ids |= {candidate.repo_id for site in result.sites for candidate in site.candidates}
    if not repo_ids:
        return {}
    with conn.begin():
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
        return _repo_name_map(conn)


def find_references_payload(
    engine: Engine, cfg: Settings, name: str, limit: int, branch: str | None = None
) -> dict[str, Any]:
    """Resolve ``name``'s call sites to ranked candidate-set definitions.

    Corpus-wide (no ``repo`` scope) over ``edge_kind="call"`` edges. Ambiguity is never
    collapsed: an ``"ambiguous"`` site's ``candidates`` list carries every ranked candidate up
    to the per-name cap (AC1). ``limit`` is the caller's already-clamped row limit (mirrors
    :func:`search_code_payload` -- clamping is the caller's responsibility, e.g. the future
    MCP tool registration in #87).
    """
    with engine.connect() as conn:
        try:
            result = resolve_references(
                conn,
                target_name=name,
                edge_kind="call",
                branch=branch,
                row_limit=limit,
                statement_timeout_ms=cfg.statement_timeout_ms,
            )
        except QueryTooBroadError:
            return {
                "query": name,
                "kind": "references",
                "symbol": name,
                "branch": branch,
                "sites": [],
                "site_count": 0,
                "resolution_summary": {"unique": 0, "ambiguous": 0, "unresolved": 0},
                "truncated": True,
                "truncation_reason": None,
                "query_too_broad": True,
            }
        name_map = _reference_repo_name_map(conn, result, cfg)

    return {
        "query": name,
        "kind": "references",
        "symbol": name,
        "branch": branch,
        "query_too_broad": False,
        **_reference_result_to_payload(result, name_map),
    }


def list_imports_payload(
    engine: Engine, cfg: Settings, repo: str, limit: int, branch: str | None = None
) -> dict[str, Any]:
    """Enumerate a repo's ``import`` edge sites (``repo`` is REQUIRED -- see D8: a corpus-wide
    listing would filter on ``edge_kind`` alone, the trailing column of
    ``ix_reference_edges_repo_kind (repo_id, edge_kind)``, which is not index-served).

    ``repo_known=False`` is a structured "no such repo" miss (mirrors ``get_file_payload``'s
    ``found: False``) -- distinct from a known repo with zero import sites, which returns
    ``repo_known=True`` and an empty ``sites`` list. Import edges are largely EXTERNAL by
    design (D3: exact dotted-path match only, no last-segment split), so most sites are
    expected to resolve ``"unresolved"`` -- that is not itself an error.
    """
    with engine.connect() as conn:
        try:
            result = resolve_references(
                conn,
                edge_kind="import",
                repo=repo,
                branch=branch,
                row_limit=limit,
                statement_timeout_ms=cfg.statement_timeout_ms,
            )
        except QueryTooBroadError:
            return {
                "query": repo,
                "kind": "imports",
                "repo": repo,
                "branch": branch,
                "repo_known": True,
                "sites": [],
                "site_count": 0,
                "resolution_summary": {"unique": 0, "ambiguous": 0, "unresolved": 0},
                "truncated": True,
                "truncation_reason": None,
                "query_too_broad": True,
            }
        name_map = _reference_repo_name_map(conn, result, cfg)

    return {
        "query": repo,
        "kind": "imports",
        "repo": repo,
        "branch": branch,
        "repo_known": result.repo_known,
        "query_too_broad": False,
        **_reference_result_to_payload(result, name_map),
    }
