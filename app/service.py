"""Payload builders for the search corpus, shared by the MCP app and the web UI app (issue #35).

Extracted from ``app/main.py`` so a second Databricks App (``webui/``) can call the exact same
search/file/repo-listing logic in-process without importing the MCP server module (which builds
a ``FastMCP``/Starlette ASGI app as an import side effect). ``app/main.py`` keeps ``_``-prefixed
aliases (``_search_code_payload = service.search_code_payload`` etc.) for its own call sites and
existing tests, so this move is a pure relocation: wire shapes, signatures, and behavior are
byte-identical to before.

Pure-ish builders (engine + config in, dict out) so unit tests pin the exact wire shape without
the SDK. Each opens its own connection INSIDE the caller's thread (never shares one across
threads). Shapes are pinned to zoekt parity (``tests/unit/test_main.py``).
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from app.config import Settings
from app.db.models import File, Repo
from app.query.parser import And, Node, Or, QueryParseError, SymbolFilter, parse
from app.search.errors import QueryTooBroadError
from app.search.grep import FileCursor, grep_search
from app.search.symbols import SymbolResult, symbol_search

# --------------------------------------------------------------------- pagination cursor


class _Unset:
    """Sentinel type for the ``cursor`` param of :func:`search_code_payload` (issue #35 A2).

    Mirrors :class:`app.search.grep._Unset`: distinguishes "no ``cursor`` argument at all"
    (every pre-#35 caller, incl. the MCP ``search_code`` tool -- gets today's exact envelope,
    no ``next_cursor`` key) from "``cursor`` explicitly supplied" (pagination mode, active even
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

    Wire format: ``{"v": 1, "r": <repo_id>, "p": <path>, "s": <content_sha>}``, compact-JSON then base64url
    WITHOUT padding (stripped ``=``; :func:`decode_cursor` re-pads before decoding). Opaque to
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

    Used on pagination continuation pages (issue #35 A2), where the symbol leg does not run
    (folding is page-1-only -- see :func:`search_code_payload`): mirrors exactly what
    ``not sym_result.no_symbol_atom`` would report from a live :func:`symbol_search` run (it
    short-circuits on this identical structural check, ``symbols.py``'s ``if not patterns``),
    so query-shape suppression (issue #31) stays consistent across every page WITHOUT reusing
    the ``sym_result is None`` timeout sentinel -- that sentinel proves a different fact (a
    timed-out DB hit), which is simply false when no DB hit was attempted at all.
    """
    match node:
        case SymbolFilter():
            return True
        case And(children=children) | Or(children=children):
            return any(_query_has_symbol_atom(child) for child in children)
        case _:
            return False


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
) -> dict[str, Any]:
    """Build the pinned ``search_code`` envelope (zoekt fields + additive signal fields).

    ``no_content_atom`` -- the query carried no content atom at all (e.g. ``lang:go``), so
    there was nothing to highlight and zero files is a *shape* outcome, not a true negative.
    ``zero_width_only_atoms`` -- content atoms were present but every one provably matches
    zero-width (e.g. ``/^/``), so every span was dropped. Mutually exclusive by construction.

    Both are grep's per-leg fact AND-ed with "the symbol leg did not answer this query", so
    neither ever fires beside results the caller can see. That suppression is what carries
    grep's own invariants (see :class:`app.search.grep.GrepResult`) up to this layer:

        If ``zero_width_only_atoms`` survives suppression then ``sym_answers`` is False, so
        ``sym_result`` is non-None with ``no_symbol_atom=True``, so ``sym_result.symbols`` is
        empty, so ``files`` comes only from grep -- which is empty by grep's invariant. QED

    Additive and permanent: agents may depend on these keys, so they can never be removed.

    ``next_cursor`` (issue #35 A2) is OMITTED from the envelope entirely when left at its
    sentinel default -- the legacy/non-pagination shape every pre-#35 caller still gets, pinned
    by ``tests/unit/test_main.py::test_envelope_keys_are_pinned_shape_plus_exactly_two``. Pass
    an explicit ``str | None`` only when ``search_code_payload`` was itself called with a
    ``cursor`` kwarg (pagination mode).
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
    into one ``files`` -> ``matches`` list grouped by ``(repo_id, path)``, matching zoekt's
    single-tool model where ``sym:`` results ride the normal envelope. A symbol match carries
    ``symbols: [{name, kind}]`` and ``line`` = the definition's first line (no highlight ``text``
    in V1, so ``grep_search("sym:X")`` -- which returns nothing highlight-driven -- is answered
    here). ``QueryParseError`` -> ``query_parse_error`` + empty files; ``QueryTooBroadError``
    (either leg) -> ``query_too_broad`` + ``truncated``. ``repo_id`` is resolved to ``Repo.name``.
    ``byte_ranges`` are our UTF-8 line-local half-open offsets (documented divergence from
    zoekt's char ``start_col``/``end_col``).

    **Query-shape suppression (issue #31).** grep's ``no_content_atom`` /
    ``zero_width_only_atoms`` are per-leg facts; both are ANDed here with ``not sym_answers``
    so neither fires on a query the symbol leg answered. ``sym:Handler`` is filter-only to grep
    but fully answered here, and ``sym:Handler /^/`` is zero-width-only to grep yet returns
    files -- flagging either would contradict the results sitting beside it and train agents to
    ignore the signal.

    ``sym_answers`` treats an UNKNOWN symbol leg (``sym_result is None``, i.e. the leg timed
    out) as answering, which is a proof rather than a precaution: ``None`` arises only from
    ``QueryTooBroadError``, which means the DB was hit, which means the ``if not patterns``
    short-circuit at ``symbols.py:173`` was False, which means the query HAD a ``sym:`` atom.
    The inverted form (``sym_result is not None and ...``) would emit a false flag on exactly
    that timed-out ``sym:`` query. Corollary: suppression can never swallow the filter-only
    case this issue exists to signal -- a query like ``file:.md`` has no ``sym:`` atom, so it
    short-circuits before any DB hit and can never reach ``sym_result is None``.

    **Pagination mode (issue #35 A2).** Omitting ``cursor`` entirely (its sentinel default)
    reproduces every byte of the pre-#35 envelope -- no ``next_cursor`` key, and a row-capped
    grep result still sets ``truncated=True``/``"row_cap"``. Supplying ``cursor`` at all --
    INCLUDING ``None`` for page 1, which the webui API always does -- switches to pagination
    mode: the envelope always carries a ``next_cursor`` (``str | null``), and a grep row-cap
    fill sets ``truncated=False`` + a non-null ``next_cursor`` instead of an error banner (see
    :func:`app.search.grep.grep_search`'s own mode gating, which this mirrors exactly). A
    garbled/tampered/version-mismatched ``cursor`` string raises :class:`CursorError`
    UNCAUGHT -- never silently restarts at page 1.

    The symbol leg (``sym:`` definitions) folds in ONLY on page 1 (pagination mode with
    ``cursor=None``); continuation pages (``cursor`` is a real cursor) skip it entirely, so a
    multi-page ``sym:X foo`` result never repeats a symbol. See :func:`_query_has_symbol_atom`
    for how shape-flag suppression stays correct on those skipped pages.
    """
    pagination_mode = not isinstance(cursor, _Unset)
    decoded_cursor: FileCursor | None = None
    if isinstance(cursor, str):
        decoded_cursor = decode_cursor(cursor)  # CursorError propagates uncaught -- see docstring

    # Built as kwargs (rather than always passing `cursor=`) so the legacy/bare call omits the
    # kwarg entirely -- grep_search's OWN sentinel default is what makes that byte-identical to
    # a pre-#35 call, mirroring the gating one level up.
    grep_kwargs: dict[str, Any] = {
        "row_limit": limit,
        "max_content_bytes": cfg.max_content_bytes,
        "statement_timeout_ms": cfg.statement_timeout_ms,
    }
    if pagination_mode:
        grep_kwargs["cursor"] = decoded_cursor

    with engine.connect() as conn:
        t0 = time.monotonic()
        try:
            result = grep_search(conn, query, **grep_kwargs)
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
            )

        # Symbol leg: sym: definitions the highlight-driven grep path cannot return. A timeout
        # here flags query_too_broad but still returns whatever grep found (partial, not a lie).
        # Note: a `sym:X foo` query runs the compiler candidate scan twice (once per leg), each
        # under its OWN statement_timeout -- so the DB-time bound is per-leg, not a single budget.
        #
        # Page-1-only (issue #35 A2): a continuation page (pagination_mode with a real
        # decoded_cursor) skips the symbol leg so folded symbols never repeat across pages.
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
        if repo_ids:
            with conn.begin():
                conn.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}"
                )
                name_map = _repo_name_map(conn)

    # Merge content + symbol matches into one file list grouped by (repo_id, path).
    merged: dict[tuple[int, str], dict[str, Any]] = {}

    def _entry(repo_id: int, path: str, lang: str | None) -> dict[str, Any]:
        entry = merged.get((repo_id, path))
        if entry is None:
            entry = {"repo_id": repo_id, "path": path, "lang": lang, "matches": []}
            merged[(repo_id, path)] = entry
        return entry

    match_count = 0
    for fm in result.files:
        entry = _entry(fm.repo_id, fm.path, fm.lang)
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
            entry = _entry(sm.repo_id, sm.path, sm.lang)
            entry["matches"].append(
                {
                    "line": sm.start_line,
                    "text": "",  # V1: line + name + kind, no def-line text (documented follow-up)
                    "byte_ranges": [],
                    "symbols": [{"name": sm.name, "kind": sm.kind}],
                }
            )
            match_count += 1  # each symbol definition is one match (its byte_ranges is empty)

    files: list[dict[str, Any]] = []
    for entry in sorted(merged.values(), key=lambda e: (e["repo_id"], e["path"])):
        # Order matches within a file by line; NULL symbol lines sort last.
        entry["matches"].sort(key=lambda m: (m["line"] is None, m["line"] or 0))
        files.append(
            {
                "repo": name_map.get(entry["repo_id"], str(entry["repo_id"])),
                "file": entry["path"],
                "language": entry["lang"],
                "branches": ["HEAD"],  # durable core has no per-branch content
                "matches": entry["matches"],
            }
        )

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
    )


def list_repos_payload(engine: Engine, cfg: Settings) -> dict[str, Any]:
    """List indexed repos with metadata, bounded by a transaction-local statement_timeout."""
    with engine.connect() as conn:
        with conn.begin():
            # SET LOCAL (int-coerced -> injection-safe) is transaction-scoped, so it never
            # leaks a statement_timeout onto the pooled connection (unlike a session-level SET).
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            rows = conn.execute(
                select(
                    Repo.name,
                    Repo.default_branch,
                    Repo.last_indexed_at,
                    Repo.last_indexed_commit,
                ).order_by(Repo.name)
            ).all()
    repos = [
        {
            "name": row.name,
            "branches": [row.default_branch] if row.default_branch else ["HEAD"],
            "index_time": row.last_indexed_at.isoformat() if row.last_indexed_at else None,
            "default_branch": row.default_branch,
            "last_indexed_commit": row.last_indexed_commit,
        }
        for row in rows
    ]
    return {"repos": repos, "count": len(repos)}


def get_file_payload(engine: Engine, cfg: Settings, repo: str, path: str) -> dict[str, Any]:
    """Fetch one file's full content by (repo name, path); a miss is a structured signal."""
    with engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            content = conn.execute(
                select(File.content)
                .join(Repo, File.repo_id == Repo.id)
                .where(Repo.name == repo, File.path == path)
            ).scalar_one_or_none()
    if content is None:
        return {"repo": repo, "path": path, "branch": "HEAD", "content": None, "found": False}
    return {"repo": repo, "path": path, "branch": "HEAD", "content": content, "found": True}
