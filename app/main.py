"""FastMCP streamable-HTTP server exposing the code-search corpus.

Adopts the author's shipped FastMCP idiom (``github.com/IceRhymers/uc-catalog-mcp``):
a stateful ``lifespan`` yielding a context dict reached via
``ctx.request_context.lifespan_context[...]``, tools that return ``str`` (``json.dumps``),
``@mcp.custom_route`` health checks, and ``app = mcp.streamable_http_app()``. Three
deliberate divergences from that reference are load-bearing:

1. **No OBO / token forwarding.** This is a single shared-corpus service principal; there
   is no per-user ``X-Forwarded-Access-Token`` path.
2. **Blocking work runs off the event loop.** ``grep_search`` runs synchronous SQL *and* a
   Python ``regex`` rescan bounded per request by a match budget (``CODE_SEARCH_MATCH_BUDGET_MS``,
   default 2000ms; a trip flags ``truncated``/``truncation_reason="match_budget"``); running it
   inline in an async handler would stall ``/health``/``/ready`` and every concurrent request.
   The ``regex`` module releases the GIL while matching, but the SQL leg still blocks, so each tool
   body is dispatched to a worker thread via ``anyio.to_thread.run_sync`` under a pool-sized
   ``CapacityLimiter(5)`` so in-flight blocking calls never oversubscribe the 5-conn pool.
3. **The engine is a process-scoped module singleton, not lifespan-owned.** A stateful
   FastMCP ``lifespan=`` is entered **once per MCP session, not once per process**
   (``lowlevel/server.py`` re-enters it on every ``Server.run()``; the streamable manager
   starts a server per session). Building the engine in the lifespan body would re-pay the
   Lakebase cold-start (``client.py:116/128/133``) and open N×5 pool connections per session,
   voiding the pool-sized limiter. So the engine lives in a lazy, ``threading.Lock``-guarded
   module singleton (``get_engine()``), built off the event loop, disposed once at process
   shutdown via ``atexit``; the per-session lifespan only *references* it and never disposes.

Recoverable conditions (``truncated``, ``query_too_broad``, ``query_parse_error``,
``regex_incompatible``, ``regex_invalid``, ``no_content_atom``, ``zero_width_only_atoms``) are
structured payload fields, never exceptions; only genuinely unexpected faults reach the
``_dispatch`` choke-point, which logs a full traceback and re-raises (never swallows). Output
shapes are pinned to the zoekt parity assertions in ``tests/unit/test_main.py``.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from sqlalchemy import select
from sqlalchemy.engine import Engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import service
from app.config import Settings, get_settings
from app.db.client import create_db_engine
from app.db.models import Repo
from app.search.grep import FileCursor
from app.search.semantic import _semantic_search_payload

logger = logging.getLogger("app.tools")

# Sized to app.db.client._DEFAULT_POOL_SIZE (5) — the SERVER default, which this process takes
# because it passes no pool_size of its own. It deliberately does NOT track the indexer, which
# derives its pool from index_concurrency. The limiter bounds in-flight blocking calls to the
# single process-wide connection pool, so a 6th concurrent call waits (bounded queueing)
# rather than oversubscribing the pool and hitting pool_timeout. The engine below is also
# module-global, so the limiter guards exactly the one pool it is sized to.
_DB_POOL_SIZE = 5
_DB_LIMITER = anyio.CapacityLimiter(_DB_POOL_SIZE)


# --------------------------------------------------------------------- engine singleton


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    """Return the process-scoped engine, building it once (lazily, race-safe).

    Callers MUST invoke this off the event loop on the first build: the first
    ``create_db_engine()`` round-trips Lakebase (``client.py:116/128/133``). The
    double-checked ``threading.Lock`` makes a first-build race between two MCP sessions safe;
    ``atexit`` disposes the engine exactly once at process shutdown. Decoupled from the MCP
    session lifecycle so the 5-conn pool is genuinely one-per-process.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                cfg = get_settings()
                engine = create_db_engine(endpoint=cfg.lakebase_endpoint)
                atexit.register(engine.dispose)  # disposed once, at process shutdown
                _engine = engine  # publish only after fully built + atexit-registered
    return _engine


# --------------------------------------------------------------------- async/sync bridge


async def _run_blocking(fn: Callable[[], Any]) -> Any:
    """Await a blocking ``fn`` on a worker thread, bounded by the pool-sized limiter."""
    return await anyio.to_thread.run_sync(fn, limiter=_DB_LIMITER)


def _signals(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the recoverable-signal fields for the observability log line."""
    return {
        "truncated": payload.get("truncated"),
        # Distinguishes which cap/budget tripped -- byte_cap/row_cap/match_budget -- from the logs.
        "truncation_reason": payload.get("truncation_reason"),
        "query_too_broad": payload.get("query_too_broad"),
        "query_parse_error": payload.get("query_parse_error"),
        # Without this, a Postgres-rejected regex is log-indistinguishable from a genuine
        # zero-result query.
        "regex_invalid": payload.get("regex_invalid"),
        # Query-shape signals: a filter-only or all-zero-width query returns zero files
        # legitimately, so without these a shape problem is indistinguishable in the logs
        # from a genuine no-match.
        "no_content_atom": payload.get("no_content_atom"),
        "zero_width_only_atoms": payload.get("zero_width_only_atoms"),
        # Without this, a flag-on-before-migrate misconfiguration is invisible in logs: every
        # semantic query returns empty and reads identically to a genuine zero-result query.
        "semantic_schema_missing": payload.get("semantic_schema_missing"),
        # Semantic filter-grammar signals: a rejected atom or an all-filters/empty query both
        # return zero results legitimately, so without these a grammar problem is
        # indistinguishable in the logs from a genuine no-match.
        "unsupported_filter": payload.get("unsupported_filter"),
        "nothing_to_embed": payload.get("nothing_to_embed"),
        # Reference-tool signals (list_imports/find_references). A repo typo otherwise reads
        # log-identically to an empty repo, and a misdirected list_imports call returns empty
        # and is otherwise indistinguishable from a genuine no-match -- the unsupported_filter
        # precedent exactly. All None-safe on payloads that do not carry them.
        "repo_known": payload.get("repo_known"),
        "unsupported_direction": payload.get("unsupported_direction"),
        "missing_repo": payload.get("missing_repo"),
        "missing_target": payload.get("missing_target"),
        # search_code's duration_ns is read HERE, before MCP projection drops the field from
        # the wire payload (see project_for_mcp below) -- so duration observability survives
        # the byte-budget work even though the field itself never reaches an MCP caller.
        "duration_ns": payload.get("duration_ns"),
        # Set only by the search_code tool wrapper's structured CursorError catch (a garbled
        # `cursor` string) -- None-safe on every other payload shape.
        "cursor_invalid": payload.get("cursor_invalid"),
    }


# ---------------------------------------------------------------- MCP response shaping
#
# Everything in this section runs ONLY on the MCP path -- app/service.py's payload builders
# and webui/ never see it (Round-1 constraint: one builder, byte-identical for both
# consumers). The pipeline per call: capture pre-projection signals -> drop webui-only
# fields (project_for_mcp) -> serialize once -> if the wire string exceeds the effective
# byte budget, per-tool tail-trim with measured re-serialization -> flag
# `truncated`/`truncation_reason="token_budget"` (extending the repo's existing
# byte_cap/row_cap/match_budget reason enum) -> attach a resume handle where plumbing exists
# (`next_cursor` for search_code, `next_start_line` for get_file). Truncation is always a
# payload fact, never an exception -- see the module docstring's recoverable-conditions
# contract; the same "never hard-error" guarantee extends to the byte budget.
#
# The budget is enforced against the EXACT wire string: `json.dumps` with its DEFAULT
# separators (this module's existing convention), never an estimate -- so a compact-separator
# switch later would only loosen the effective budget, never violate it.

# Floor so a hostile/typo'd `max_bytes=1` request param cannot force a truncator into a
# degenerate (or infinite) trim loop; still far below the 100_000 default.
_MIN_MAX_BYTES = 1024


def _effective_budget(max_bytes: int | None, cfg: Settings) -> int:
    """Resolve the byte budget for one call: `max_bytes` clamps the env default DOWN, never up.

    `None` or a non-positive value means "no per-request override" -> the env-configured
    ceiling. Otherwise clamped to `[_MIN_MAX_BYTES, cfg.mcp_max_response_bytes]` -- a request
    can shrink the ceiling but never raise it above the server-configured maximum, and never
    below the floor.
    """
    if max_bytes is None or max_bytes <= 0:
        return cfg.mcp_max_response_bytes
    return max(_MIN_MAX_BYTES, min(max_bytes, cfg.mcp_max_response_bytes))


def project_for_mcp(tool: str, payload: dict[str, Any]) -> None:
    """Mutate ``payload`` in place, dropping webui-only fields from the MCP wire response.

    Table-driven (a future drop is a one-line addition) and None-safe on skeletal/minimal
    payloads (every access is a defensive ``.get`` that no-ops when the key/list is absent) --
    the wrapper tests' fakes return bare dicts like ``{"query": q}``. Today only
    ``search_code`` carries any of the three dropped fields (verified against every other
    builder's shape): ``duration_ns`` (rendered nowhere; ``_signals()`` already read it before
    this runs), per-file ``content_sha`` (near-zero LLM value; the cursor keeps its own copy
    inside the opaque token), and per-match ``byte_ranges`` (webui highlighting only, derivable
    from ``text``). ``permalink_branch``/``commit``/ranking metadata/``rrf_score``/``similarity``
    are deliberately KEPT (spec constraint) -- this function only ever removes the three named
    fields, nothing else.
    """
    payload.pop("duration_ns", None)
    if tool != "search_code":
        return
    for file_entry in payload.get("files") or []:
        file_entry.pop("content_sha", None)
        for match in file_entry.get("matches") or []:
            match.pop("byte_ranges", None)


def _fit_list(items: Sequence[Any], budget_for_items: int) -> int:
    """Return the largest prefix length of ``items`` whose combined serialized size fits.

    Prefix-sum over ``len(json.dumps(item)) + 2`` (a separator allowance for the ``", "``
    joining each item in the enclosing JSON array) against ``budget_for_items``. Exact against
    default-separator ``json.dumps``, not an estimate.
    """
    used = 0
    for index, item in enumerate(items):
        cost = len(json.dumps(item)) + 2
        if used + cost > budget_for_items:
            return index
        used += cost
    return len(items)


def _make_tail_trim_truncator(
    list_key: str, recompute: Callable[[dict[str, Any]], None]
) -> Callable[[dict[str, Any], int], None]:
    """Build a truncator that tail-trims ``payload[list_key]`` to fit ``budget``.

    Shared by the four tools with no resume-handle plumbing (``list_repos``,
    ``find_references``, ``list_imports``, ``semantic_search``): drop the tail of the
    dominant list (relevance-ranked for ``semantic_search``, so the tail is the least
    relevant), recompute the list's derived count/summary fields, and flag
    ``truncated``/``truncation_reason``. None-safe: an absent or empty list still flags
    (lossy, no resume handle for these four) rather than raising.
    """

    def _truncate(payload: dict[str, Any], budget: int) -> None:
        payload.setdefault("truncated", False)
        payload.setdefault("truncation_reason", None)
        items: list[Any] = payload.get(list_key) or []
        if not items:
            payload["truncated"] = True
            if payload.get("truncation_reason") is None:
                payload["truncation_reason"] = "token_budget"
            return

        envelope_overhead = len(json.dumps(payload)) - sum(
            len(json.dumps(item)) + 2 for item in items
        )
        budget_for_items = max(0, budget - envelope_overhead)
        keep = _fit_list(items, budget_for_items)
        payload[list_key] = items[:keep]
        recompute(payload)
        payload["truncated"] = True
        if payload.get("truncation_reason") is None:
            payload["truncation_reason"] = "token_budget"

        # Safety net: the per-item "+2" separator allowance is a close but not
        # byte-for-byte-guaranteed estimate once `recompute` changes derived scalars (e.g. a
        # count's digit width). Re-verify against the real wire string and keep halving until
        # it fits or the list is empty -- bounded (log2(len(items)) iterations), never infinite.
        while keep > 0 and len(json.dumps(payload)) > budget:
            keep //= 2
            payload[list_key] = items[:keep]
            recompute(payload)

    return _truncate


def _recompute_list_repos(payload: dict[str, Any]) -> None:
    payload["count"] = len(payload.get("repos") or [])


def _recompute_reference_sites(payload: dict[str, Any]) -> None:
    """Recompute ``site_count``/``resolution_summary`` from the surviving (trimmed) sites --
    both currently derive from the returned list, so recomputing keeps them internally
    consistent after a tail-trim."""
    sites: list[dict[str, Any]] = payload.get("sites") or []
    payload["site_count"] = len(sites)
    summary = {"unique": 0, "ambiguous": 0, "unresolved": 0}
    for site in sites:
        resolution = site.get("resolution")
        if resolution in summary:
            summary[resolution] += 1
    payload["resolution_summary"] = summary


def _recompute_semantic_results(payload: dict[str, Any]) -> None:
    payload["count"] = len(payload.get("results") or [])


def _resolve_repo_id(engine: Engine, cfg: Settings, name: str) -> int | None:
    """One bounded ``repos.name -> id`` SELECT, fired only when byte-budget truncation of
    ``search_code`` needs to synthesize a resume cursor -- the built payload resolves
    ``repo_id`` to a name and drops the id, so the MCP layer must resolve it back.

    A no-row result (reachable: the builder itself falls back to ``str(repo_id)`` when a repo
    id has no name, ``service.py:764``, so the payload's ``repo`` string may match no
    ``repos.name``) or any unexpected fault returns ``None`` -- the caller then leaves the
    response flagged ``truncated``/``"token_budget"`` with no cursor (an honest lossy
    truncation), never raising through ``_dispatch``.
    """
    try:
        with engine.connect() as conn:
            with conn.begin():
                conn.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}"
                )
                return conn.execute(select(Repo.id).where(Repo.name == name)).scalar_one_or_none()
    except Exception:
        logger.warning("search_code cursor resolve failed for repo=%r", name, exc_info=True)
        return None


def _make_search_code_truncator(
    engine: Engine, cfg: Settings
) -> Callable[[dict[str, Any], int, list[tuple[str, int]] | None], None]:
    """Build the ``search_code`` truncator: a closure over ``engine``/``cfg`` so an actual
    byte-budget trim can synthesize ``next_cursor`` by resolving the last kept file's repo
    name back to an id (the built payload only carries the name, not the id ``grep``'s cursor
    needs).

    Pure tail-trim of ``files`` in the payload's existing ``(repo_id, path, content_sha)`` sort
    order (never reordered to protect symbol-bearing files) -- the kept files are always a
    maximal contiguous prefix of that order, so the last kept file is a valid resume point:
    every candidate after it (kept or dropped) sorts strictly later, exactly the invariant
    ``search_code``'s cursor-seek predicate resumes on. ``snapshot`` is
    ``[(content_sha, span_count), ...]`` captured by ``_shape_response`` BEFORE
    ``project_for_mcp`` strips ``content_sha``/``byte_ranges`` from ``payload["files"]`` --
    trimming here is positional against that snapshot, not content-aware.

    Progress guarantee: whenever the untruncated payload had at least one file, at least one
    file is ALWAYS kept, even if that single file's own serialized size alone exceeds
    ``budget`` -- mirroring ``_shape_get_file_response``'s single-oversized-line edge. Without
    this floor, a single huge file (e.g. one file with thousands of matches) would fit zero
    items, yielding ``files: []`` with no ``next_cursor`` to resume from: an unrecoverable dead
    end that contradicts the "always advance" contract this tool promises callers.
    """

    def _truncate(
        payload: dict[str, Any], budget: int, snapshot: list[tuple[str, int]] | None
    ) -> None:
        files: list[dict[str, Any]] = payload.get("files") or []
        snapshot = snapshot or []
        if not files:
            payload["truncated"] = True
            if payload.get("truncation_reason") is None:
                payload["truncation_reason"] = "token_budget"
            payload["next_cursor"] = None
            return

        def _apply(keep: int) -> None:
            kept = files[:keep]
            payload["files"] = kept
            payload["file_count"] = len(kept)
            payload["match_count"] = sum(count for _sha, count in snapshot[:keep])

        envelope_overhead = len(json.dumps(payload)) - sum(len(json.dumps(f)) + 2 for f in files)
        budget_for_items = max(0, budget - envelope_overhead)
        keep = _fit_list(files, budget_for_items)
        if keep == 0:
            keep = 1  # progress guarantee -- see docstring; this page may exceed budget
        _apply(keep)
        payload["truncated"] = True
        if payload.get("truncation_reason") is None:
            payload["truncation_reason"] = "token_budget"

        # Safety net (mirrors _make_tail_trim_truncator's): re-verify against the real wire
        # string and shrink further if the "+2" separator estimate under-counted. Floored at 1,
        # never 0, so the progress guarantee above can never be undone here.
        while keep > 1 and len(json.dumps(payload)) > budget:
            keep = max(1, keep // 2)
            _apply(keep)

        last_file = payload["files"][keep - 1]
        last_sha = snapshot[keep - 1][0] if keep - 1 < len(snapshot) else None
        repo_name = last_file.get("repo")
        path = last_file.get("file")
        next_cursor: str | None = None
        if repo_name is not None and path is not None and last_sha:
            repo_id = _resolve_repo_id(engine, cfg, repo_name)
            if repo_id is not None:
                next_cursor = service.encode_cursor(
                    FileCursor(repo_id=repo_id, path=path, content_sha=last_sha)
                )
        payload["next_cursor"] = next_cursor

    return _truncate


# Static table for the four tools with no resume-handle plumbing. search_code and get_file
# are NOT here: search_code's truncator must close over engine/cfg (the cursor-synthesis
# SELECT), and get_file's shaping is not a "trim a list" operation at all (see
# _shape_get_file_response) -- both are wired per-request by their own tool wrapper instead.
_TRUNCATORS: dict[str, Callable[[dict[str, Any], int], None]] = {
    "list_repos": _make_tail_trim_truncator("repos", _recompute_list_repos),
    "find_references": _make_tail_trim_truncator("sites", _recompute_reference_sites),
    "list_imports": _make_tail_trim_truncator("sites", _recompute_reference_sites),
    "semantic_search": _make_tail_trim_truncator("results", _recompute_semantic_results),
}


def _shape_response(
    tool: str,
    payload: dict[str, Any],
    budget: int,
    truncator: Callable[..., None] | None,
) -> tuple[str, dict[str, Any]]:
    """Project, measure, and (if needed) truncate ``payload`` for the MCP wire.

    Returns ``(body, log_fields)`` where ``log_fields`` carries ``pre_bytes``/``post_bytes``
    (pre- and post-projection serialized sizes, for ``_dispatch``'s telemetry line) and the
    pre-projection ``signals`` dict (so ``duration_ns`` observability survives projection
    dropping the field). Never raises: an irreducible over-budget envelope (a giant echoed
    scalar with nothing left to trim) is returned flagged rather than raising -- forward
    progress for the caller always wins over strict budget enforcement.
    """
    pre_bytes = len(json.dumps(payload))
    signals = _signals(payload)

    # search_code only: snapshot (content_sha, span_count) per file BEFORE projection strips
    # content_sha/byte_ranges -- the truncator needs both to synthesize a resume cursor and to
    # recompute match_count over the surviving tail (symbol matches count 1, grep matches count
    # len(byte_ranges), exactly mirroring service.py's own match_count arithmetic).
    snapshot: list[tuple[str, int]] | None = None
    if tool == "search_code":
        snapshot = [
            (
                entry.get("content_sha") or "",
                sum(
                    len(match.get("byte_ranges") or ()) or (1 if match.get("symbols") else 0)
                    for match in (entry.get("matches") or [])
                ),
            )
            for entry in (payload.get("files") or [])
        ]

    project_for_mcp(tool, payload)
    body = json.dumps(payload)
    if len(body) <= budget or truncator is None:
        return body, {"pre_bytes": pre_bytes, "post_bytes": len(body), "signals": signals}

    if tool == "search_code":
        truncator(payload, budget, snapshot)
    else:
        truncator(payload, budget)

    body = json.dumps(payload)
    return body, {"pre_bytes": pre_bytes, "post_bytes": len(body), "signals": signals}


def _line_cost(line: str) -> int:
    """The exact JSON-escaped body-byte cost of ``line`` (excluding the two quote chars
    ``json.dumps`` adds around any standalone string) -- JSON string escaping has no
    cross-character interaction, so this is exact when lines are later joined, not approximate.
    """
    return len(json.dumps(line)) - 2


def _fit_lines(lines: Sequence[str], budget_for_content: int) -> int:
    """Largest prefix of ``lines`` whose ``"\\n"``-rejoined content fits ``budget_for_content``
    JSON-string-body bytes. Each line after the first adds 2 bytes for the re-appended
    ``"\\n"`` (which ``json.dumps`` encodes as the two characters ``\\n``) that ``split("\\n")``
    stripped and a per-line ``json.dumps`` would not otherwise capture.
    """
    used = 0
    for index, line in enumerate(lines):
        cost = _line_cost(line) + (2 if index > 0 else 0)
        if used + cost > budget_for_content:
            return index
        used += cost
    return len(lines)


def _shape_get_file_response(
    payload: dict[str, Any], start_line: int, budget: int
) -> tuple[str, dict[str, Any]]:
    """Slice ``payload["content"]`` to a ``start_line``-anchored page fitting ``budget``.

    Splits with ``content.split("\\n")`` -- the SAME rule ``grep.py:436`` uses for
    ``search_code`` line numbers, so ``get_file`` pages stay congruent with search match lines
    on every input, including form feeds and ``U+2028``/``U+2029`` (which ``str.splitlines()``
    would wrongly treat as line breaks for this purpose). Reassembly re-appends ``"\\n"`` to
    every segment except the last, which is byte-exact for CRLF (the ``\\r`` stays attached to
    its own segment) and no-trailing-newline files alike: pages joined with ``"\\n"`` across
    page boundaries reconstruct the exact original content.

    A miss (``found: false``) never truncates: ``start_line`` echoes, ``next_start_line`` is
    ``null``, ``truncated`` is ``False``. On a hit, at least one line is always returned even if
    it alone exceeds ``budget`` -- always making forward progress for the caller outranks strict
    budget enforcement for that documented, degenerate edge (a single line larger than the whole
    budget); ``next_start_line`` still advances past it.
    """
    pre_bytes = len(json.dumps(payload))
    signals = _signals(payload)
    project_for_mcp("get_file", payload)
    start_line = max(1, start_line)

    if not payload.get("found"):
        payload["start_line"] = start_line
        payload["next_start_line"] = None
        payload["truncated"] = False
        payload["truncation_reason"] = None
        body = json.dumps(payload)
        return body, {"pre_bytes": pre_bytes, "post_bytes": len(body), "signals": signals}

    content = payload.get("content") or ""
    lines = content.split("\n")  # grep.py:436 congruence -- NOT str.splitlines()
    total_lines = len(lines)
    start_idx = min(start_line - 1, total_lines)
    page_lines = lines[start_idx:]

    payload["start_line"] = start_line
    payload["content"] = "\n".join(page_lines)
    payload["truncated"] = False
    payload["truncation_reason"] = None
    payload["next_start_line"] = None
    body = json.dumps(payload)
    if len(body) <= budget:
        return body, {"pre_bytes": pre_bytes, "post_bytes": len(body), "signals": signals}

    # envelope_overhead = everything except the content string's own escaped-body bytes
    # (excluding its two outer quotes) -- the exact per-line cost unit _fit_lines uses.
    content_cost = len(json.dumps(payload["content"])) - 2
    envelope_overhead = len(body) - content_cost
    budget_for_content = max(0, budget - envelope_overhead)
    keep = _fit_lines(page_lines, budget_for_content)
    if keep == 0 and page_lines:
        keep = 1  # progress guarantee: always return >= 1 line, even oversized (documented edge)

    def _apply(k: int) -> None:
        kept_lines = page_lines[:k]
        payload["content"] = "\n".join(kept_lines)
        next_idx = start_idx + k
        payload["next_start_line"] = (next_idx + 1) if next_idx < total_lines else None

    _apply(keep)
    payload["truncated"] = True
    payload["truncation_reason"] = "token_budget"
    body = json.dumps(payload)
    while keep > 1 and len(body) > budget:
        keep -= 1
        _apply(keep)
        body = json.dumps(payload)
    return body, {"pre_bytes": pre_bytes, "post_bytes": len(body), "signals": signals}


def _cursor_invalid_payload(query: str, error: Exception) -> dict[str, Any]:
    """A garbled/tampered/version-mismatched ``cursor`` string: a structured, remedy-bearing
    rejection -- the repo's recoverable-payload idiom (mirrors ``unsupported_filter``) -- never
    an uncaught :class:`~app.service.CursorError` through ``_dispatch``. Carries the full
    pinned empty envelope so a caller's existing key access never KeyErrors.
    """
    return {
        "query": query,
        "file_count": 0,
        "match_count": 0,
        "duration_ns": 0,
        "files": [],
        "truncated": False,
        "truncation_reason": None,
        "regex_incompatible": False,
        "regex_invalid": None,
        "query_too_broad": False,
        "query_parse_error": None,
        "no_content_atom": False,
        "zero_width_only_atoms": False,
        "next_cursor": None,
        "cursor_invalid": True,
        "reason": f"invalid cursor: {error}",
    }


async def _dispatch(
    name: str,
    build: Callable[[], dict[str, Any]],
    *,
    max_bytes: int | None = None,
    shape: Callable[[dict[str, Any], int], tuple[str, dict[str, Any]]] | None = None,
) -> str:
    """Run a tool's blocking ``build`` off-loop, shape the result to the byte budget, log the
    outcome, and return the serialized wire string.

    The single choke-point every tool routes through: it times the call, logs the signal set,
    the pre-/post-shaping response byte sizes, and limiter/pool saturation on success, and on
    an UNEXPECTED fault logs the full traceback (``logger.exception``) then re-raises — the
    fault is never swallowed. Recoverable conditions are turned into payload fields inside
    ``build``, so only genuine faults land here.

    ``shape`` defaults to :func:`_shape_response` bound to ``name``'s static truncator (the
    four no-resume-handle tools); ``search_code``/``get_file`` pass their own ``shape`` closure
    (over ``engine``/``cfg``/``start_line``) since their shaping needs request-scoped state a
    static per-name lookup cannot carry. Both ``build`` and ``shape`` run INSIDE the same
    worker-thread call (``_run_blocking``), so the multi-MB ``json.dumps`` calls and any
    truncation-time DB round trip never touch the event loop.
    """
    t0 = time.monotonic()
    cfg = get_settings()
    budget = _effective_budget(max_bytes, cfg)
    shape_fn = shape or (
        lambda payload, b: _shape_response(name, payload, b, _TRUNCATORS.get(name))
    )

    def _run() -> tuple[str, dict[str, Any]]:
        payload = build()
        return shape_fn(payload, budget)

    try:
        body, log_fields = await _run_blocking(_run)
    except Exception:
        logger.exception("tool=%s failed", name)
        raise
    logger.info(
        "tool=%s duration_ms=%.1f response_bytes_pre=%d response_bytes=%d signals=%s "
        "limiter_borrowed=%d/%d",
        name,
        (time.monotonic() - t0) * 1e3,
        log_fields["pre_bytes"],
        log_fields["post_bytes"],
        log_fields["signals"],
        _DB_LIMITER.borrowed_tokens,
        _DB_LIMITER.total_tokens,
    )
    return body


# ------------------------------------------------------------------------ payload builders
#
# The payload builders (clamp_limit / search_code_payload / list_repos_payload /
# get_file_payload) live in app/service.py so a second Databricks App (webui/) can call them
# in-process without importing this module's ASGI-app-building side effects. These
# aliases keep this module's own call sites and existing tests unchanged; they are the exact
# same function objects, so tests monkeypatching their collaborators must patch `service.*`
# (function globals resolve in the DEFINING module, not here).
_clamp_limit = service.clamp_limit
_search_code_payload = service.search_code_payload
_list_repos_payload = service.list_repos_payload
_get_file_payload = service.get_file_payload
_find_references_payload = service.find_references_payload
_list_imports_payload = service.list_imports_payload


def _append_branch_atom(query: str, branch: str) -> str:
    """Append ``branch:"<branch>"`` to ``query``: the ``search_code`` ``branch`` param is
    sugar for the ``branch:`` query atom, quoted so ``/``, ``.``, and rare spaces are
    scanner-safe. ``app.query.parser._read_quoted`` only special-cases ``\\"`` -> ``"``, so
    the sole character that needs escaping here is an embedded ``"``.
    """
    escaped = branch.replace('"', '\\"')
    return f'{query} branch:"{escaped}"'.strip()


def _append_commit_atom(query: str, commit: str) -> str:
    """Append ``commit:<hash>`` to ``query``: the ``search_code`` ``commit`` param is sugar for
    the ``commit:`` atom, mirroring :func:`_append_branch_atom`. No quoting -- a git hash is hex
    only (7--40 chars) and carries no scanner-special char; a malformed value is rejected by the
    parser and surfaces as ``query_parse_error`` rather than an exception.
    """
    return f"{query} commit:{commit}".strip()


# ------------------------------------------------------------------------------- lifespan


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Per-MCP-session lifespan: reference the process-scoped engine singleton.

    Builds the singleton off the event loop on first entry (the first ``create_db_engine``
    round-trips Lakebase) so a cold start never stalls the loop / health probes. Does NOT
    dispose the engine — this re-enters per MCP session; disposal is ``atexit``'s job.
    """
    cfg = get_settings()
    engine = await anyio.to_thread.run_sync(get_engine)
    yield {"engine": engine, "config": cfg}


# ---------------------------------------------------------------------------------- tools
#
# Tools and routes are plain module functions registered onto a fresh ``FastMCP`` by
# ``build_mcp()`` below (NOT decorated onto one module-global instance). A
# ``streamable_http_app`` caches a single ``StreamableHTTPSessionManager`` whose ``run()`` may
# be entered only once per instance, so a per-instance factory is what lets each test (and any
# future multi-mount) get its own session manager instead of reusing a spent one.


async def search_code(
    query: str,
    ctx: Context,
    limit: int = 200,
    branch: str | None = None,
    commit: str | None = None,
    cursor: str | None = None,
    max_bytes: int | None = None,
) -> str:
    """Search the indexed corpus with a zoekt-style query; returns file-grouped line matches.

    Supports ``repo:``/``file:``/``lang:``/``sym:``/``branch:``/``commit:`` filters, ``case:yes``,
    boolean AND (whitespace) / OR, ``/regex/`` patterns, and negation: a ``-`` prefixing any
    term or field (``-foo``, ``-repo:acme``, ``-/regex/``, ``-(a b)``) EXCLUDES it -- files whose
    compiled query matches an excluded term are dropped from candidacy. Without ``branch:``/
    ``commit:`` (or the params below), results are scoped to each repo's default branch.
    ``branch:<name>`` restricts to files whose indexed branches include ``<name>`` (exact match,
    not a glob/regex).

    ``commit:<hash>`` scopes to whatever (repo, branch) heads are indexed at that git commit --
    a full 40-char SHA or a hex prefix of >= 7 chars (matched git-style against
    ``repo_branches.last_indexed_commit``). It has two moods: a bare ``commit:<hash>`` returns
    only a ``resolved`` list (which repo/branch each match, plus the full SHA and index time) with
    empty ``files`` -- a reverse lookup; ``commit:<hash> <terms>`` runs a normal search scoped to
    the resolved heads and returns both ``files`` and ``resolved``. A hash that matches no indexed
    branch returns empty ``files`` with ``commit_not_indexed: true`` (never an unfiltered search).

    ``branch``/``commit`` are convenience params equivalent to appending ``branch:"<value>"`` /
    ``commit:<value>`` to ``query``. ``limit`` caps the number of files scanned (clamped to a
    server maximum). Recoverable conditions surface as fields (``query_parse_error``,
    ``query_too_broad``, ``truncated``, ``regex_incompatible``, ``regex_invalid``,
    ``no_content_atom``, ``zero_width_only_atoms``). ``truncated``/``truncation_reason`` also
    cover the Python match-budget trip (``truncation_reason="match_budget"``): a pathological
    pattern that exhausts the per-request CPU budget stops scanning and returns a flagged
    partial result rather than pinning a worker. The middle two explain an empty result that is
    NOT a true negative: ``no_content_atom`` means the query carried no affirmative content atom
    to highlight -- either a filter-only query (e.g. ``lang:go`` alone) or one that is entirely
    negated (e.g. ``-foo`` alone: an exclusion is never a highlight) -- and
    ``zero_width_only_atoms`` means every atom it carried matches zero-width (e.g. ``/^/``). A
    query mixing content with an exclusion (e.g. ``foo -bar``) highlights only the affirmative
    term; excluded terms never appear as matches. ``no_content_atom`` does not distinguish "no
    atom at all" from "fully negated" -- recover which one it was from the echoed ``query``
    field, if it matters. ``regex_invalid`` carries the Postgres error message when a
    ``/regex/``, ``repo:``, ``file:``, or ``sym:`` pattern is not a valid Postgres POSIX ARE
    (e.g. ``/[/``) -- distinct from ``regex_incompatible``, which means Python ``regex`` (not
    Postgres) rejected an otherwise-valid pattern and only degrades highlighting.

    This tool always runs in pagination mode: page 1 omits ``cursor`` (or passes ``null``),
    and every response carries ``next_cursor`` (``str | null``) -- resume a traversal by
    passing the previous response's ``next_cursor`` back as ``cursor``. **Behavior change**:
    because of this, a plain row-cap fill now reports ``truncated: false`` + a non-null
    ``next_cursor`` instead of the old ``truncated: true``/``truncation_reason: "row_cap"``
    (there is a next page, not an error) -- ``truncated: true``/``"token_budget"`` is the
    byte-budget signal below, and a match-budget trip still reports
    ``truncation_reason="match_budget"``. A garbled/tampered/unrecognized ``cursor`` string
    never raises: it comes back as a structured ``cursor_invalid: true`` payload with a remedy
    ``reason`` and the normal empty envelope.

    ``max_bytes`` caps the serialized response size in bytes (default
    ``CODE_SEARCH_MCP_MAX_RESPONSE_BYTES``, ~4 bytes/token; a request value only clamps the
    server ceiling DOWN, never up). An over-budget response is truncated to fit -- tail-trimmed
    in the payload's existing file order -- and flagged ``truncated: true``/
    ``truncation_reason: "token_budget"`` with a synthesized ``next_cursor`` so a caller can
    keep paging through the CONTENT matches losslessly; a ``sym:`` query's page-1-only symbol
    definitions that land in a truncated tail are lost from that traversal (flagged, not
    silently dropped) since the symbol leg never re-runs on a continuation page. At least one
    file is always kept and a ``next_cursor`` always synthesized when there was at least one
    file to begin with -- a single file whose own serialized size alone exceeds ``max_bytes``
    (e.g. one file with thousands of matches) is still returned alone, with that one response
    exceeding the budget, rather than coming back as an empty, unresumable dead end.
    """
    lc = ctx.request_context.lifespan_context
    engine, cfg = lc["engine"], lc["config"]
    limit = _clamp_limit(limit, cfg)
    if branch:
        query = _append_branch_atom(query, branch)
    if commit:
        query = _append_commit_atom(query, commit)

    def _build() -> dict[str, Any]:
        try:
            return _search_code_payload(engine, cfg, query, limit, cursor=cursor)
        except service.CursorError as error:
            return _cursor_invalid_payload(query, error)

    truncator = _make_search_code_truncator(engine, cfg)
    return await _dispatch(
        "search_code",
        _build,
        max_bytes=max_bytes,
        shape=lambda payload, budget: _shape_response("search_code", payload, budget, truncator),
    )


async def semantic_search(
    query: str,
    ctx: Context,
    limit: int = 50,
    branch: str | None = None,
    max_bytes: int | None = None,
) -> str:
    """Semantic + BM25 hybrid search: rank indexed chunks by relevance to a free-text query.

    Unlike :func:`search_code` (zoekt grammar over lines), this takes natural-language text --
    but it ALSO accepts the same ``repo:``/``file:``/``lang:``/``branch:`` scoping atoms
    ``search_code`` does, with lexical-parity matching semantics: ``repo:<pattern>`` and
    ``file:<pattern>`` match as case-insensitive regular expressions against the repo name /
    file path; ``lang:<name>`` matches a normalized (stripped, lowercased) exact language;
    ``branch:<name>`` matches exact branch membership. Filter atoms are stripped from ``query``
    before ranking; the REMAINING natural-language text is what gets embedded and searched --
    filter-then-rank, never post-filtered, so a highly selective filter narrows the candidate
    pool the ranking draws from, not just the results shown.

    ``sym:``, ``case:``, ``commit:``, bare ``/regex/``, and lexical negation (a leading ``-``,
    e.g. ``-foo``) have no meaning here and are REJECTED (``unsupported_filter`` in the payload,
    naming the atom as ``"-"`` for negation, with a remedy in ``reason``): ``sym:``/``commit:``
    -> symbol/commit-scoped search is lexical-only, use :func:`search_code`; ``case:`` -> case
    sensitivity does not apply to semantic ranking, remove it; a bare ``/.../`` -> quote the term
    to search it as literal text (this also catches innocent absolute-path prose like
    ``/etc/nginx.conf`` -- quote it: ``"/etc/nginx.conf"``); ``-`` -> exclusion has no meaning
    for a ranked natural-language query, remove it (this is deterministic and checked BEFORE any
    embedding or database work -- a negated query is never silently embedded with the ``-``
    stripped or reinterpreted). A query that is only filters, or empty/whitespace-only, leaves
    nothing to embed and returns ``nothing_to_embed: true`` with no embedding call made.

    ``repo:``/``file:`` filter values ARE matched as Postgres regular expressions (unlike a
    bare token, which is only rejected if written ``/like/this/``): a Postgres-invalid pattern
    there (e.g. ``repo:[``) returns ``regex_invalid`` set to the Postgres error message, with a
    remedy in ``reason`` -- fix the pattern rather than resubmitting the query verbatim.

    ``branch`` is sugar for a ``branch:`` atom -- conjunctive with any ``branch:`` atom already
    in ``query`` (mirrors ``search_code``'s ``branch`` param) -- restricting to files whose
    indexed branches include the given name (exact match); with no branch given anywhere,
    results are scoped to each repo's default branch. ``limit`` caps the number of ranked
    chunks returned (clamped to a server maximum). Registered unconditionally, but gated at
    runtime: when semantic search is explicitly disabled (``CODE_SEARCH_SEMANTIC_ENABLED=0``)
    it returns a clean ``semantic_enabled: false`` payload -- never a 500/503 -- and touches
    neither the database nor the embedder (nor does it parse ``query`` at all). Each result
    carries ``repo``, ``file``, ``chunk_index``, ``content``, ``start_line``/``end_line``
    (1-based inclusive; null for chunks indexed before line tracking), ``rrf_score`` (the fused
    rank score), and ``similarity`` (raw cosine similarity against the query embedding, defined
    as ``1 - cosine_distance``; ``null`` for chunks with no embedding) -- ``rrf_score`` alone is
    not comparable across queries, ``similarity`` is.

    ``max_bytes`` caps the serialized response size in bytes (default
    ``CODE_SEARCH_MCP_MAX_RESPONSE_BYTES``, ~4 bytes/token; a request value only clamps the
    server ceiling DOWN, never up). An over-budget response tail-trims the (already
    relevance-ranked) ``results`` list -- dropping the least relevant first -- and flags
    ``truncated: true``/``truncation_reason: "token_budget"``; there is no resume handle for
    this tool, so a truncated response is lossy (re-run with a smaller ``limit`` or a narrower
    query to see what was cut).
    """
    lc = ctx.request_context.lifespan_context
    engine, cfg = lc["engine"], lc["config"]
    limit = _clamp_limit(limit, cfg)
    return await _dispatch(
        "semantic_search",
        lambda: _semantic_search_payload(engine, cfg, query, limit, branch),
        max_bytes=max_bytes,
    )


async def list_repos(ctx: Context, max_bytes: int | None = None) -> str:
    """List every indexed repository with its branches and per-branch last-indexed metadata.

    ``max_bytes`` caps the serialized response size in bytes (default
    ``CODE_SEARCH_MCP_MAX_RESPONSE_BYTES``, ~4 bytes/token; a request value only clamps the
    server ceiling DOWN, never up). An over-budget response tail-trims ``repos`` and flags
    ``truncated: true``/``truncation_reason: "token_budget"``; there is no resume handle for
    this tool, so a truncated response is lossy.
    """
    lc = ctx.request_context.lifespan_context
    return await _dispatch(
        "list_repos",
        lambda: _list_repos_payload(lc["engine"], lc["config"]),
        max_bytes=max_bytes,
    )


async def get_file(
    repo: str,
    path: str,
    ctx: Context,
    branch: str | None = None,
    start_line: int = 1,
    max_bytes: int | None = None,
) -> str:
    """Return a file's content by repository name and path (miss -> ``found:false``).

    ``branch`` scopes the lookup to the content version indexed on that branch (one path may
    have several); omitted, it resolves to the repo's default branch. The resolved branch is
    echoed back in the payload.

    ``start_line`` (1-based; values ``< 1`` clamp to 1) pages through large files: the response
    always carries ``start_line`` (echo) and ``next_start_line`` (the next page's ``start_line``
    when the file continues past what was returned, ``null`` when the tail fits). Content is
    split on ``"\\n"`` -- the SAME rule ``search_code``'s match ``line`` numbers use, so a
    ``get_file`` page's line numbers stay congruent with search results. Paging through every
    page from ``start_line=1`` and rejoining each page's ``content`` with ``"\\n"`` reconstructs
    the file byte-exactly (CRLF and no-trailing-newline files included). ``max_bytes`` caps the
    serialized response size in bytes (default ``CODE_SEARCH_MCP_MAX_RESPONSE_BYTES``, ~4
    bytes/token; a request value only clamps the server ceiling DOWN, never up); an over-budget
    page is cut to the largest whole-line prefix that fits and flagged ``truncated: true``/
    ``truncation_reason: "token_budget"``. Edge case: a single line whose JSON-encoded size
    alone exceeds ``max_bytes`` (e.g. a minified one-line file) is still returned alone --
    flagged, with the response exceeding the budget for that one call -- because always making
    forward progress outranks strict enforcement for that degenerate case; ``next_start_line``
    still advances past it.
    """
    lc = ctx.request_context.lifespan_context
    start_line = max(1, start_line)
    return await _dispatch(
        "get_file",
        lambda: _get_file_payload(lc["engine"], lc["config"], repo, path, branch),
        max_bytes=max_bytes,
        shape=lambda payload, budget: _shape_get_file_response(payload, start_line, budget),
    )


async def find_references(
    symbol: str,
    ctx: Context,
    limit: int = 200,
    branch: str | None = None,
    max_bytes: int | None = None,
) -> str:
    """Find candidate call sites of ``symbol`` corpus-wide, each with its ranked definitions.

    CANDIDATE-SET semantics, NOT compiler-precise references: results are name-resolved over
    raw ``call`` edges (grep-not-LSP). Each site is a place that calls something NAMED
    ``symbol``; its ``candidates`` are the ``symbols`` definitions that name could plausibly
    mean, ranked -- never a single authoritative binding. Ambiguity is preserved in full, never
    collapsed to one answer.

    ``symbol`` is matched exactly against the callee's rightmost identifier (``a.b.f()`` and
    ``self.f()`` both match ``f``). ``branch`` scopes BOTH the call site's file and each
    candidate's file (exact ``branches`` membership); omitted, both fall back to each repo's
    default branch. ``limit`` caps the number of call sites scanned (clamped to a server
    maximum).

    Payload: ``symbol``, ``branch``, ``site_count``, ``sites``, a top-level ``resolution_summary``
    histogram (``{"unique":N,"ambiguous":N,"unresolved":N}``), ``truncated``, and
    ``query_too_broad``. Each entry in ``sites`` carries ``repo``, ``file``, ``line``,
    ``edge_kind`` (``"call"``), ``target_name``, ``enclosing_symbol`` (``{"name","kind"}`` of the
    function/class the call sits in, or ``null`` for module scope), ``resolution``
    (``"unique"``=1 candidate, ``"ambiguous"``=2+, ``"unresolved"``=0), ``candidate_count`` (the
    TRUE pre-cap count -- correct even when the candidate list is capped), ``candidates_truncated``,
    and a ranked ``candidates`` list. Each candidate carries ``repo``, ``file``, ``line``,
    ``name``, ``kind``, and the ranking signals ``same_repo`` / ``same_file`` / ``kind_match``.

    Composition -- "what tests cover symbol X": call ``find_references(X)`` and client-side
    filter ``sites`` by your test-path convention (e.g. ``file`` starts with ``"tests/"``); each
    surviving site's ``enclosing_symbol`` names the covering test. No separate tool is needed.

    ``max_bytes`` caps the serialized response size in bytes (default
    ``CODE_SEARCH_MCP_MAX_RESPONSE_BYTES``, ~4 bytes/token; a request value only clamps the
    server ceiling DOWN, never up). An over-budget response tail-trims ``sites`` (recomputing
    ``site_count``/``resolution_summary`` from the survivors) and flags ``truncated: true``/
    ``truncation_reason: "token_budget"``; there is no resume handle for this tool, so a
    truncated response is lossy (re-run with a smaller ``limit`` to see what was cut).
    """
    lc = ctx.request_context.lifespan_context
    engine, cfg = lc["engine"], lc["config"]
    limit = _clamp_limit(limit, cfg)
    return await _dispatch(
        "find_references",
        lambda: _find_references_payload(engine, cfg, symbol, limit, branch),
        max_bytes=max_bytes,
    )


async def list_imports(
    ctx: Context,
    repo: str | None = None,
    target: str | None = None,
    direction: str = "imports",
    branch: str | None = None,
    limit: int = 200,
    max_bytes: int | None = None,
) -> str:
    """Enumerate ``import`` edge sites in one of two directions (candidate-set semantics).

    ``direction="imports"`` (default): list the import sites IN a repo -- **``repo`` is
    REQUIRED** (a corpus-wide import listing is not index-served and is out of scope). An
    optional ``target`` narrows to sites importing that exact dotted path.
    ``direction="imported_by"``: find who imports a module -- **``target`` is REQUIRED** (the
    exact dotted path, e.g. ``"os.path"``), searched corpus-wide; an optional ``repo`` narrows
    to importers within that one repo.

    Invalid input returns a STRUCTURED payload, never an error: an unknown ``direction`` sets
    ``unsupported_direction`` (echoing the value); ``imports`` with no ``repo`` sets
    ``missing_repo``; ``imported_by`` with no ``target`` sets ``missing_target``. Each also
    carries a remedy ``reason`` and an empty result envelope.

    Import edges target the FULL dotted path as written (no last-segment split), so most point
    at external/stdlib modules and resolve ``"unresolved"`` -- that is expected and correct, not
    an error. ``repo_known=False`` is a structured "no such repo" miss (distinct from a known
    repo with zero import sites: ``repo_known=True`` with empty ``sites``); it is always ``True``
    when no ``repo`` scope was requested.

    Payload: ``kind`` (``"imports"``), ``direction``, ``repo``, ``target``, ``repo_known``,
    ``branch``, ``site_count``, ``sites``, ``resolution_summary``, ``truncated``, and
    ``query_too_broad``. Each ``sites`` entry has the same shape as ``find_references`` (``repo``,
    ``file``, ``line``, ``edge_kind`` = ``"import"``, ``target_name``, ``enclosing_symbol`` |
    ``null`` for module scope, ``resolution``, ``candidate_count``, ``candidates_truncated``,
    ranked ``candidates``). ``limit`` caps the sites scanned (clamped to a server maximum).

    ``max_bytes`` caps the serialized response size in bytes (default
    ``CODE_SEARCH_MCP_MAX_RESPONSE_BYTES``, ~4 bytes/token; a request value only clamps the
    server ceiling DOWN, never up). An over-budget response tail-trims ``sites`` (recomputing
    ``site_count``/``resolution_summary`` from the survivors) and flags ``truncated: true``/
    ``truncation_reason: "token_budget"``; there is no resume handle for this tool, so a
    truncated response is lossy (re-run with a smaller ``limit`` to see what was cut).
    """
    lc = ctx.request_context.lifespan_context
    engine, cfg = lc["engine"], lc["config"]
    limit = _clamp_limit(limit, cfg)
    return await _dispatch(
        "list_imports",
        lambda: _list_imports_payload(
            engine, cfg, repo, limit, branch, target=target, direction=direction
        ),
        max_bytes=max_bytes,
    )


# ------------------------------------------------------------------------- health / ready


async def health(request: Request) -> JSONResponse:
    """Liveness: zero-DB, always 200 while the process is up."""
    return JSONResponse({"status": "ok"})


async def ready(request: Request) -> JSONResponse:
    """Readiness: a bounded probe against a real protected table.

    ``SELECT 1 FROM repos LIMIT 1`` (not a bare ``SELECT 1``) forces the SELECT grant check,
    so a ``CAN_CONNECT``-only role with no reader grant — or an unreachable Lakebase — surfaces
    as 503 instead of shipping green. Runs off-loop under the same limiter as the tools.
    """
    cfg = get_settings()

    def probe() -> None:
        engine = get_engine()  # module singleton; no per-session accessor race
        with engine.connect() as conn:
            with conn.begin():
                conn.exec_driver_sql(
                    f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}"
                )
                conn.exec_driver_sql("SELECT 1 FROM repos LIMIT 1")

    try:
        await _run_blocking(probe)
        return JSONResponse({"status": "ready"})
    except Exception as error:
        # Log the detail server-side; return a generic body so an unauthenticated probe caller
        # never sees the raw DB error (which can echo the Lakebase host / schema / relation names).
        logger.warning("readiness probe failed: %r", error)
        return JSONResponse({"status": "unready"}, status_code=503)


# --------------------------------------------------------------------------- ASGI export

# Server-level ``instructions`` land verbatim in every client's system prompt (via
# ``initialize``), so this is the always-on channel: it answers "should I engage this server at
# all" and routes the four entry-level tools. It deliberately does NOT enumerate
# ``find_references``/``list_imports`` -- those are discoverable from the tool list once an
# agent has engaged -- and it routes discovery through ``list_repos`` rather than asserting a
# corpus exists, so it stays true against a freshly forked template with an empty index.
SERVER_INSTRUCTIONS = """
Indexed code search across many repositories -- including repositories that are NOT
checked out in the current working directory.

Use these tools whenever a request names a repository, project, file, or symbol you
cannot find locally. Local file tools only see the working directory; do not conclude
that something does not exist based on a local search alone -- check list_repos first
to see what is indexed.

Then: search_code for exact text, regex, or symbol matches; semantic_search for
natural-language questions about behavior; get_file to read a known path in full.
""".strip()

# All six tools are genuinely read-only against an external, open-world index (semantic-accuracy
# grounds only -- Claude Code does not auto-approve on ``readOnlyHint``, so no routing credit is
# claimed). Module-level constant because inlining ``ToolAnnotations(...)`` six times pushes
# every registration line past ``line-length = 100`` (pyproject.toml).
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def build_mcp() -> FastMCP:
    """Build a fresh ``FastMCP`` with tools/routes registered and its own single-use
    ``StreamableHTTPSessionManager``. Split out of ``create_app()`` so tool metadata
    (``instructions``, ``list_tools()``) is reachable and unit-testable without standing up a
    full HTTP session -- the Starlette app returned by ``streamable_http_app()`` does not expose
    the ``FastMCP`` object it was built from. Each call still yields a fresh instance, preserving
    the single-use session-manager constraint described in the tools comment above."""
    mcp = FastMCP("code-search", instructions=SERVER_INSTRUCTIONS, lifespan=lifespan)
    mcp.tool(title="Search Code", annotations=_READ_ONLY)(search_code)
    mcp.tool(title="Semantic Search", annotations=_READ_ONLY)(semantic_search)
    mcp.tool(title="List Indexed Repositories", annotations=_READ_ONLY)(list_repos)
    mcp.tool(title="Get File", annotations=_READ_ONLY)(get_file)
    mcp.tool(title="Find References", annotations=_READ_ONLY)(find_references)
    mcp.tool(title="List Imports", annotations=_READ_ONLY)(list_imports)
    mcp.custom_route("/health", methods=["GET"])(health)
    mcp.custom_route("/ready", methods=["GET"])(ready)
    return mcp


def create_app() -> Starlette:
    """Build a fresh MCP ASGI app. Production uses the module ``app`` below; tests call this
    per test so each gets an unspent session manager (see the tools comment above)."""
    return build_mcp().streamable_http_app()


app = create_app()
