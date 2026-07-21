"""FastAPI web UI backend: a second Databricks App over the same corpus (issue #35).

Sibling to ``app/main.py`` (the MCP server): imports the SAME payload builders from
``app.service`` so search/file/repo-listing behavior is exactly the MCP tools' behavior,
just wired to HTTP GET routes and a submit-based frontend instead of MCP tool calls.

Deliberately owns its OWN engine singleton, ``CapacityLimiter``, and off-loop dispatch
pattern (copied from ``app/main.py:68-147``) rather than importing ``app.main``'s
module-globals: the two apps are independent Databricks Apps processes, each with its own DB
connection pool sized to its own capacity limiter, and importing ``app.main`` would build the
MCP ``FastMCP``/Starlette app as an unwanted import side effect (``app/main.py``'s module-level
``app = create_app()``).

``get_engine``/``get_settings`` are used as FastAPI dependencies (``Depends(...)``) rather than
being called directly from route bodies, so tests can override them
(``app.dependency_overrides[...]``) with a fake engine/deterministic settings without touching
the real Lakebase connection or environment -- the same test seam ``app/main.py``'s tests get
via the lifespan-context dict, adapted to FastAPI's own DI mechanism.
"""

from __future__ import annotations

import atexit
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app import service
from app.config import Settings, get_settings
from app.db.client import create_db_engine
from app.service import CursorError

logger = logging.getLogger("webui")

# Sized to app.db.client._DEFAULT_POOL_SIZE (5), which this process takes because it passes no
# pool_size of its own to create_db_engine -- see the identical reasoning in app/main.py. Bounds
# in-flight blocking calls to the single process-wide connection pool this app owns.
_DB_POOL_SIZE = 5
_DB_LIMITER = anyio.CapacityLimiter(_DB_POOL_SIZE)

_FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"


# --------------------------------------------------------------------- engine singleton


_engine: Engine | None = None
_engine_lock = threading.Lock()


def get_engine() -> Engine:
    """Return this app's process-scoped engine, building it once (lazily, race-safe).

    Copied from ``app/main.py:get_engine`` verbatim in spirit: a lazy, ``threading.Lock``
    -guarded module singleton (never built inside a FastAPI ``lifespan``, since the first
    ``create_db_engine()`` round-trips Lakebase and would otherwise stall startup or, worse,
    re-pay the cold start per worker), disposed once at process shutdown via ``atexit``.
    """
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                cfg = get_settings()
                engine = create_db_engine(endpoint=cfg.lakebase_endpoint)
                atexit.register(engine.dispose)
                _engine = engine
    return _engine


async def _run_blocking(fn: Callable[[], Any]) -> Any:
    """Await a blocking ``fn`` on a worker thread, bounded by the pool-sized limiter."""
    return await anyio.to_thread.run_sync(fn, limiter=_DB_LIMITER)


EngineDep = Annotated[Engine, Depends(get_engine)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# ------------------------------------------------------------------------------- static SPA


class SPAStaticFiles(StaticFiles):
    """Serve the built SPA, falling back to ``index.html`` for any unmatched NON-API path.

    Client-side routes (e.g. ``/file?repo=x&path=y``) are not real files on disk; without this
    override, a hard refresh or a direct deep link would 404 instead of loading the SPA shell
    and letting ``src/router.ts`` hydrate from ``location``. This is mounted at ``/`` AFTER all
    API routers in :func:`create_app`, so a REGISTERED ``/api/*``/``/health``/``/ready`` route
    is matched first and never reaches here -- but an UNREGISTERED ``/api/*`` path (a typo, a
    retired route) also falls through to this mount, and without the ``/api/`` exclusion below
    would silently return the SPA's HTML shell with a 200, not a 404, to an API caller
    expecting JSON. Re-raising leaves it to Starlette's default ``HTTPException`` handler,
    which renders ``{"detail": ...}`` as JSON -- so ``/api/*`` 404s are JSON, everything else
    still gets the SPA fallback.
    """

    async def get_response(self, path: str, scope: Scope) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not scope["path"].startswith("/api/"):
                return await super().get_response("index.html", scope)
            raise


# ------------------------------------------------------------------------------------ routes


async def health() -> dict[str, str]:
    """Liveness: zero-DB, always 200 while the process is up."""
    return {"status": "ok"}


async def ready(engine: EngineDep, cfg: SettingsDep) -> JSONResponse:
    """Readiness: a bounded probe against a real protected table.

    ``SELECT 1 FROM repos LIMIT 1`` (not a bare ``SELECT 1``) forces the SELECT grant check, so
    a ``CAN_CONNECT``-only role with no reader grant -- or an unreachable Lakebase -- surfaces
    as 503 instead of shipping green. Mirrors ``app/main.py:ready``.
    """

    def probe() -> None:
        with engine.connect() as conn:
            with conn.begin():
                timeout_ms = int(cfg.statement_timeout_ms)
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout_ms}")
                conn.exec_driver_sql("SELECT 1 FROM repos LIMIT 1")

    try:
        await _run_blocking(probe)
        return JSONResponse({"status": "ready"})
    except Exception as error:
        # Log the detail server-side; return a generic body so an unauthenticated probe caller
        # never sees the raw DB error (which can echo the Lakebase host/schema/relation names).
        logger.warning("readiness probe failed: %r", error)
        return JSONResponse({"status": "unready"}, status_code=503)


async def api_search(
    engine: EngineDep,
    cfg: SettingsDep,
    q: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query()] = 0,
    cursor: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Search the indexed corpus. Always pagination mode: ``cursor`` is passed explicitly
    (``None`` for page 1), so the response always carries ``next_cursor`` (issue #35 A2).

    Unlike :class:`app.service.CursorError` (raised uncaught -- see the ``except`` below),
    ``QueryParseError`` never escapes :func:`service.search_code_payload`: it is caught
    INSIDE the builder and folded into the returned payload's ``query_parse_error`` field
    (a structured signal, matching the MCP tools' "recoverable conditions are payload
    fields, never exceptions" contract -- see ``app/main.py``'s module docstring). So the
    400 mapping here inspects the payload rather than catching an exception.

    A ``cursor`` decodes structurally fine (passes :func:`service.decode_cursor`'s checks) but
    can still carry a NUL byte in its ``path`` -- decode_cursor validates shape/type, not byte
    content. That NUL then reaches a bound SQL parameter in the resumed candidate scan, where
    Postgres itself rejects it (``sqlalchemy.exc.DataError``: "PostgreSQL text fields cannot
    contain NUL (0x00) bytes"), 500ing an attacker-controlled input instead of 400ing it.
    """
    clamped = service.clamp_limit(limit, cfg)
    try:
        payload = await _run_blocking(
            lambda: service.search_code_payload(engine, cfg, q, clamped, cursor=cursor)
        )
    except CursorError as error:
        raise HTTPException(status_code=400, detail={"error": str(error)}) from error
    except DataError as error:
        raise HTTPException(status_code=400, detail={"error": "invalid parameter"}) from error
    if payload["query_parse_error"] is not None:
        raise HTTPException(status_code=400, detail={"error": payload["query_parse_error"]})
    return payload


async def api_file(
    engine: EngineDep,
    cfg: SettingsDep,
    repo: Annotated[str, Query(min_length=1)],
    path: Annotated[str, Query(min_length=1)],
) -> dict[str, Any]:
    """Return one file's full content by (repo name, path); a miss is a 404.

    A ``repo``/``path`` containing a NUL byte reaches a bound SQL parameter in
    :func:`service.get_file_payload`'s lookup, where Postgres rejects it
    (``sqlalchemy.exc.DataError``) rather than simply matching zero rows -- without this,
    that 500s an attacker-controlled input instead of 400ing it.
    """
    try:
        payload = await _run_blocking(lambda: service.get_file_payload(engine, cfg, repo, path))
    except DataError as error:
        raise HTTPException(status_code=400, detail={"error": "invalid parameter"}) from error
    if not payload["found"]:
        detail = {"error": f"{repo}/{path} not found", "repo": repo, "path": path}
        raise HTTPException(status_code=404, detail=detail)
    return payload


async def api_repos(engine: EngineDep, cfg: SettingsDep) -> dict[str, Any]:
    """List every indexed repository with its branch and last-indexed metadata."""
    return await _run_blocking(lambda: service.list_repos_payload(engine, cfg))


async def api_semantic_status(cfg: SettingsDep) -> dict[str, Any]:
    """Flag-only visibility probe for the Semantic nav tab: zero-DB, zero-SDK by construction.

    Does NOT probe schema presence (``semantic_schema_missing`` surfaces at query time instead,
    inside the ``/api/semantic`` payload) -- the flag is a product decision (visibility), the
    schema is operator progress (an in-tab message), and conflating them would couple nav
    rendering to DB health for no benefit (issue #36 Fork 4).
    """
    return {"semantic_enabled": cfg.semantic_enabled}


async def api_semantic(
    engine: EngineDep,
    cfg: SettingsDep,
    q: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query()] = 50,
    branch: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Hybrid semantic + BM25 search over indexed chunks (issue #36).

    ``limit`` defaults to 50 -- parity with the MCP ``semantic_search`` tool's own default
    (``app/main.py``), NOT ``/api/search``'s ``0 -> row_limit`` convention: the two surfaces
    should return the same result set for the same defaulted call.

    Disabled (``semantic_enabled: false``) and not-migrated (``semantic_schema_missing: true``)
    payloads pass through unchanged as 200 bodies -- recoverable conditions are payload fields,
    never HTTP errors (mirrors ``app/main.py``'s dispatch contract). Only malformed input and
    backend faults become HTTP errors: a NUL byte in ``q``/``branch`` reaching a bound SQL
    parameter raises ``DataError`` -> 400 (same rationale as the existing routes); anything else
    (e.g. the embedding endpoint's SDK/auth/network failures, which are arbitrary exception
    types) is logged with a full traceback server-side and mapped to a generic 502 so a raw
    error body never echoes endpoint/host detail (mirrors ``ready()``'s no-leak policy).
    """
    clamped = service.clamp_limit(limit, cfg)
    try:
        return await _run_blocking(
            lambda: service.semantic_search_payload(engine, cfg, q, clamped, branch)
        )
    except DataError as error:
        raise HTTPException(status_code=400, detail={"error": "invalid parameter"}) from error
    except Exception as error:
        if isinstance(error, HTTPException):
            raise
        logger.exception("semantic search failed")
        raise HTTPException(
            status_code=502, detail={"error": "semantic search backend unavailable"}
        ) from error


# ----------------------------------------------------------------------- security headers


async def _add_security_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
    """Set a minimal security header set on every response (API routes and the SPA alike).

    ``X-Content-Type-Options: nosniff`` stops a browser from MIME-sniffing a response into
    executable content; ``X-Frame-Options: DENY`` blocks this app from being framed
    (clickjacking). A full Content-Security-Policy is deliberately deferred: Shiki's syntax
    highlighting emits inline per-token ``style="color:..."`` attributes
    (``webui/frontend/src/components/CodeBlock.tsx``), which a strict CSP would need
    ``style-src 'unsafe-inline'`` (or a nonce/hash scheme) to allow without breaking file
    view -- tracked as follow-up work in ``docs/runbooks/webui.md``.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# --------------------------------------------------------------------------- ASGI export


def create_app() -> FastAPI:
    """Build the FastAPI app: security headers, then API routers, then the SPA mount."""
    app = FastAPI(title="code-search webui")
    app.middleware("http")(_add_security_headers)

    app.get("/health")(health)
    app.get("/ready")(ready)
    app.get("/api/search")(api_search)
    app.get("/api/file")(api_file)
    app.get("/api/repos")(api_repos)
    app.get("/api/semantic/status")(api_semantic_status)
    app.get("/api/semantic")(api_semantic)

    if _FRONTEND_DIST.is_dir():
        app.mount("/", SPAStaticFiles(directory=_FRONTEND_DIST, html=True), name="spa")
    else:
        logger.warning(
            "frontend dist directory not found at %s; run `make webui-build`. "
            "API routes are still served; the SPA is not.",
            _FRONTEND_DIST,
        )

    return app


app = create_app()
