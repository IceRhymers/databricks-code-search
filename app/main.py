"""FastMCP streamable-HTTP server exposing the code-search corpus (issue #11).

Adopts the author's shipped FastMCP idiom (``github.com/IceRhymers/uc-catalog-mcp``):
a stateful ``lifespan`` yielding a context dict reached via
``ctx.request_context.lifespan_context[...]``, tools that return ``str`` (``json.dumps``),
``@mcp.custom_route`` health checks, and ``app = mcp.streamable_http_app()``. Three
deliberate divergences from that reference are load-bearing:

1. **No OBO / token forwarding.** This is a single shared-corpus service principal; there
   is no per-user ``X-Forwarded-Access-Token`` path.
2. **Blocking work runs off the event loop.** ``grep_search`` runs synchronous SQL *and* a
   Python ``re`` rescan whose CPU is uncapped (``grep.py:45-49``); running it inline in an
   async handler would stall ``/health``/``/ready`` and every concurrent request. Each tool
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
``regex_incompatible``) are structured payload fields, never exceptions; only genuinely
unexpected faults reach the ``_dispatch`` choke-point, which logs a full traceback and
re-raises (never swallows). Output shapes are pinned to the zoekt parity fixtures in
``.omc/plans/zoekt-reference-shapes.md``.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.server.fastmcp import Context, FastMCP
from sqlalchemy import select
from sqlalchemy.engine import Engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import Settings, get_settings
from app.db.client import create_db_engine
from app.db.models import File, Repo
from app.query.parser import QueryParseError
from app.search.errors import QueryTooBroadError
from app.search.grep import grep_search
from app.search.symbols import SymbolResult, symbol_search

logger = logging.getLogger("app.tools")

# Sized to app.db.client._POOL_SIZE (5): the limiter bounds in-flight blocking calls to the
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
        "query_too_broad": payload.get("query_too_broad"),
        "query_parse_error": payload.get("query_parse_error"),
    }


async def _dispatch(name: str, build: Callable[[], dict[str, Any]]) -> str:
    """Run a tool's blocking ``build`` off-loop, log its outcome, and serialize to ``str``.

    The single choke-point every tool routes through: it times the call, logs the signal set
    and limiter/pool saturation on success, and on an UNEXPECTED fault logs the full traceback
    (``logger.exception``) then re-raises — the fault is never swallowed. Recoverable
    conditions are turned into payload fields inside ``build``, so only genuine faults land
    here.
    """
    t0 = time.monotonic()
    try:
        payload = await _run_blocking(build)
    except Exception:
        logger.exception("tool=%s failed", name)
        raise
    logger.info(
        "tool=%s duration_ms=%.1f signals=%s limiter_borrowed=%d/%d",
        name,
        (time.monotonic() - t0) * 1e3,
        _signals(payload),
        _DB_LIMITER.borrowed_tokens,
        _DB_LIMITER.total_tokens,
    )
    return json.dumps(payload)


def _clamp_limit(limit: int, cfg: Settings) -> int:
    """Clamp a caller-supplied ``limit``: ``<=0`` -> default; ``> max`` -> hard cap."""
    if limit <= 0:
        return cfg.row_limit
    return min(limit, cfg.max_row_limit)


# ------------------------------------------------------------------------ payload builders
#
# Pure-ish builders (engine + config in, dict out) so unit tests pin the exact wire shape
# without the SDK. Each opens its own connection INSIDE the worker thread (never shares one
# across threads). Shapes are pinned to zoekt parity (.omc/plans/zoekt-reference-shapes.md).


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
) -> dict[str, Any]:
    """Build the pinned ``search_code`` envelope (zoekt fields + additive signal fields)."""
    return {
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
    }


def _search_code_payload(engine: Engine, cfg: Settings, query: str, limit: int) -> dict[str, Any]:
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
    """
    with engine.connect() as conn:
        t0 = time.monotonic()
        try:
            result = grep_search(
                conn,
                query,
                row_limit=limit,
                max_content_bytes=cfg.max_content_bytes,
                statement_timeout_ms=cfg.statement_timeout_ms,
            )
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
            )

        # Symbol leg: sym: definitions the highlight-driven grep path cannot return. A timeout
        # here flags query_too_broad but still returns whatever grep found (partial, not a lie).
        query_too_broad = False
        try:
            sym_result: SymbolResult | None = symbol_search(
                conn,
                query,
                row_limit=limit,
                statement_timeout_ms=cfg.statement_timeout_ms,
            )
        except QueryTooBroadError:
            sym_result = None
            query_too_broad = True

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

    sym_truncated = sym_result.truncated if sym_result is not None else False
    truncated = result.truncated or sym_truncated or query_too_broad
    truncation_reason = result.truncation_reason or (
        sym_result.truncation_reason if sym_result is not None else None
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
    )


def _list_repos_payload(engine: Engine, cfg: Settings) -> dict[str, Any]:
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


def _get_file_payload(engine: Engine, cfg: Settings, repo: str, path: str) -> dict[str, Any]:
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
# ``create_app()`` below (NOT decorated onto one module-global instance). A
# ``streamable_http_app`` caches a single ``StreamableHTTPSessionManager`` whose ``run()`` may
# be entered only once per instance, so a per-instance factory is what lets each test (and any
# future multi-mount) get its own session manager instead of reusing a spent one.


async def search_code(query: str, ctx: Context, limit: int = 200) -> str:
    """Search the indexed corpus with a zoekt-style query; returns file-grouped line matches.

    Supports ``repo:``/``file:``/``lang:``/``sym:`` filters, ``case:yes``, boolean AND
    (whitespace) / OR, and ``/regex/`` patterns. ``limit`` caps the number of files scanned
    (clamped to a server maximum). Recoverable conditions surface as fields
    (``query_parse_error``, ``query_too_broad``, ``truncated``, ``regex_incompatible``).
    """
    lc = ctx.request_context.lifespan_context
    engine, cfg = lc["engine"], lc["config"]
    limit = _clamp_limit(limit, cfg)
    return await _dispatch("search_code", lambda: _search_code_payload(engine, cfg, query, limit))


async def list_repos(ctx: Context) -> str:
    """List every indexed repository with its branch and last-indexed metadata."""
    lc = ctx.request_context.lifespan_context
    return await _dispatch("list_repos", lambda: _list_repos_payload(lc["engine"], lc["config"]))


async def get_file(repo: str, path: str, ctx: Context) -> str:
    """Return the full content of a file by repository name and path (miss -> ``found:false``)."""
    lc = ctx.request_context.lifespan_context
    return await _dispatch(
        "get_file", lambda: _get_file_payload(lc["engine"], lc["config"], repo, path)
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


def create_app() -> Starlette:
    """Build a fresh MCP ASGI app: a new ``FastMCP`` with tools/routes registered and its own
    single-use ``StreamableHTTPSessionManager``. Production uses the module ``app`` below; tests
    call this per test so each gets an unspent session manager (see the tools comment above)."""
    mcp = FastMCP("code-search", lifespan=lifespan)
    mcp.tool()(search_code)
    mcp.tool()(list_repos)
    mcp.tool()(get_file)
    mcp.custom_route("/health", methods=["GET"])(health)
    mcp.custom_route("/ready", methods=["GET"])(ready)
    return mcp.streamable_http_app()


app = create_app()
