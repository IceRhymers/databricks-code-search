"""FastMCP streamable-HTTP server exposing the code-search corpus.

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
``regex_incompatible``, ``no_content_atom``, ``zero_width_only_atoms``) are structured payload
fields, never exceptions; only genuinely unexpected faults reach the ``_dispatch``
choke-point, which logs a full traceback and re-raises (never swallows). Output shapes are
pinned to the zoekt parity assertions in ``tests/unit/test_main.py``.
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
from sqlalchemy.engine import Engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import service
from app.config import get_settings
from app.db.client import create_db_engine
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
        "query_too_broad": payload.get("query_too_broad"),
        "query_parse_error": payload.get("query_parse_error"),
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
# ``create_app()`` below (NOT decorated onto one module-global instance). A
# ``streamable_http_app`` caches a single ``StreamableHTTPSessionManager`` whose ``run()`` may
# be entered only once per instance, so a per-instance factory is what lets each test (and any
# future multi-mount) get its own session manager instead of reusing a spent one.


async def search_code(
    query: str,
    ctx: Context,
    limit: int = 200,
    branch: str | None = None,
    commit: str | None = None,
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
    ``query_too_broad``, ``truncated``, ``regex_incompatible``, ``no_content_atom``,
    ``zero_width_only_atoms``). The last two explain an empty result that is NOT a true negative:
    ``no_content_atom`` means the query carried no affirmative content atom to highlight --
    either a filter-only query (e.g. ``lang:go`` alone) or one that is entirely negated (e.g.
    ``-foo`` alone: an exclusion is never a highlight) -- and ``zero_width_only_atoms`` means
    every atom it carried matches zero-width (e.g. ``/^/``). A query mixing content with an
    exclusion (e.g. ``foo -bar``) highlights only the affirmative term; excluded terms never
    appear as matches. ``no_content_atom`` does not distinguish "no atom at all" from "fully
    negated" -- recover which one it was from the echoed ``query`` field, if it matters.
    """
    lc = ctx.request_context.lifespan_context
    engine, cfg = lc["engine"], lc["config"]
    limit = _clamp_limit(limit, cfg)
    if branch:
        query = _append_branch_atom(query, branch)
    if commit:
        query = _append_commit_atom(query, commit)
    return await _dispatch("search_code", lambda: _search_code_payload(engine, cfg, query, limit))


async def semantic_search(
    query: str, ctx: Context, limit: int = 50, branch: str | None = None
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
    """
    lc = ctx.request_context.lifespan_context
    engine, cfg = lc["engine"], lc["config"]
    limit = _clamp_limit(limit, cfg)
    return await _dispatch(
        "semantic_search", lambda: _semantic_search_payload(engine, cfg, query, limit, branch)
    )


async def list_repos(ctx: Context) -> str:
    """List every indexed repository with its branches and per-branch last-indexed metadata."""
    lc = ctx.request_context.lifespan_context
    return await _dispatch("list_repos", lambda: _list_repos_payload(lc["engine"], lc["config"]))


async def get_file(repo: str, path: str, ctx: Context, branch: str | None = None) -> str:
    """Return the full content of a file by repository name and path (miss -> ``found:false``).

    ``branch`` scopes the lookup to the content version indexed on that branch (one path may
    have several); omitted, it resolves to the repo's default branch. The resolved branch is
    echoed back in the payload.
    """
    lc = ctx.request_context.lifespan_context
    return await _dispatch(
        "get_file", lambda: _get_file_payload(lc["engine"], lc["config"], repo, path, branch)
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
    mcp.tool()(semantic_search)
    mcp.tool()(list_repos)
    mcp.tool()(get_file)
    mcp.custom_route("/health", methods=["GET"])(health)
    mcp.custom_route("/ready", methods=["GET"])(ready)
    return mcp.streamable_http_app()


app = create_app()
