"""End-to-end tests for the FastMCP server over streamable HTTP (issue #11).

Requires a running Postgres with the standard PG* env set. As with ``test_grep.py``, in this
repo that Postgres exists only as CI's service container, so these tests are CI-only and were
validated locally by lint/type-check + ``--collect-only``, not execution.

Two seams are load-bearing (both proven in review):

* **Schema visibility (Critic M1).** The fixture seeds a throwaway schema and sets
  ``search_path`` on its *own* admin connection, but the server builds its *own* engine whose
  connections default to ``search_path=public``. So BEFORE the server engine is built we set
  ``os.environ["PGOPTIONS"] = "-c search_path=<schema>,public"`` — libpq applies it to every
  server connection (local psycopg path) — and reset the process-scoped engine singleton
  (``app.main._engine = None``) so the server builds a fresh engine under that PGOPTIONS.
* **DNS-rebinding (Critic M-B).** ``FastMCP`` defaults ``host=127.0.0.1`` and auto-enables
  DNS-rebinding protection allowing only port-bearing localhost/127.0.0.1. A ``Host`` of
  ``app`` or a portless ``localhost`` 421s before any tool runs, so the client uses the
  port-bearing ``http://localhost:8000`` base URL and ``/mcp`` path.

``httpx.ASGITransport`` does NOT run ASGI lifespan events; without ``asgi_lifespan``'s
``LifespanManager`` the streamable session manager raises "Task group is not initialized".
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from sqlalchemy import insert, text

from app import main
from app.db.client import create_db_engine
from app.db.models import Base, File, Repo, Symbol

SCHEMA_PREFIX = "test_mcp"
BASE_URL = "http://localhost:8000"  # port-bearing: clears FastMCP DNS-rebinding protection
MCP_URL = f"{BASE_URL}/mcp"


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _reset_engine() -> None:
    """Drop the process-scoped engine singleton so the next build honors current PGOPTIONS."""
    if main._engine is not None:
        main._engine.dispose()
    main._engine = None
    # Settings are process-cached; clear so a future test that mutates CODE_SEARCH_*/
    # LAKEBASE_ENDPOINT is not served a stale frozen Settings from the first access.
    main.get_settings.cache_clear()


def _set_pgoptions(schema: str) -> str | None:
    """Point every server connection at ``schema`` via libpq PGOPTIONS; return the prior value."""
    prev = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = f"-c search_path={schema},public"
    _reset_engine()  # force the server to rebuild under the new search_path
    return prev


def _restore_pgoptions(prev: str | None) -> None:
    if prev is None:
        os.environ.pop("PGOPTIONS", None)
    else:
        os.environ["PGOPTIONS"] = prev
    _reset_engine()


@pytest.fixture
def seeded_schema() -> Iterator[str]:
    """Throwaway schema + durable-core DDL + the deterministic grep corpus, PGOPTIONS-visible."""
    schema = _unique(SCHEMA_PREFIX)
    admin_engine = create_db_engine()
    conn = admin_engine.connect()
    prev_pgoptions: str | None = None
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.commit()

        Base.metadata.create_all(bind=conn)
        conn.commit()

        acme_id = conn.execute(
            insert(Repo)
            .values(name="acme/widgets", default_branch="main", last_indexed_commit="abc123")
            .returning(Repo.id)
        ).scalar_one()
        conn.execute(insert(Repo).values(name="beta/tools", default_branch="main"))
        handler_file_id = conn.execute(
            insert(File)
            .values(
                repo_id=acme_id,
                path="src/handler.go",
                lang="go",
                content="package main\nfunc Handler() {}\n// foo lives here and foo again\n",
            )
            .returning(File.id)
        ).scalar_one()
        # A symbol row so a sym: query exercises the folded symbol-search leg end-to-end.
        conn.execute(
            insert(Symbol).values(
                file_id=handler_file_id,
                repo_id=acme_id,
                name="Handler",
                kind="function",
                start_line=2,
            )
        )
        conn.commit()

        # Point the server engine at this schema BEFORE it is built, and reset the singleton.
        prev_pgoptions = _set_pgoptions(schema)
        yield schema
    finally:
        _restore_pgoptions(prev_pgoptions)
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        admin_engine.dispose()


@pytest.fixture
def empty_schema() -> Iterator[str]:
    """A schema WITHOUT the durable tables: ``/ready``'s ``SELECT 1 FROM repos`` must fail (503).

    Stands in for the "CAN_CONNECT but no SELECT grant" case (M-A): both make the protected-table
    probe raise, which is exactly what a bare ``SELECT 1`` would have hidden.
    """
    schema = _unique(SCHEMA_PREFIX)
    admin_engine = create_db_engine()
    conn = admin_engine.connect()
    prev_pgoptions: str | None = None
    try:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.commit()
        prev_pgoptions = _set_pgoptions(schema)
        yield schema
    finally:
        _restore_pgoptions(prev_pgoptions)
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        admin_engine.dispose()


def _make_client_factory(app: Any) -> Any:
    """Return an McpHttpClientFactory-shaped factory bound to ``app`` (in-process ASGITransport).

    A fresh ``app`` per test is load-bearing: a ``streamable_http_app`` caches one
    ``StreamableHTTPSessionManager`` whose ``run()`` may be entered only once per instance, so
    reusing the module ``app`` across tests makes the second ``LifespanManager`` entry raise.
    """

    def factory(
        *,
        headers: dict[str, str] | None = None,
        timeout: Any = None,
        auth: Any = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    return factory


def _tool_json(result: Any) -> dict[str, Any]:
    """Extract and JSON-decode a tool result's text content block."""
    return json.loads(result.content[0].text)


@pytest.mark.e2e
async def test_streamable_http_tools_and_health(seeded_schema: str) -> None:
    app = main.create_app()  # fresh session manager per test (see _make_client_factory)
    async with LifespanManager(app):
        async with streamablehttp_client(
            MCP_URL, httpx_client_factory=_make_client_factory(app)
        ) as (
            r,
            w,
            _,
        ):
            async with ClientSession(r, w) as session:
                await session.initialize()

                names = {t.name for t in (await session.list_tools()).tools}
                assert {"search_code", "list_repos", "get_file"} <= names

                search = _tool_json(await session.call_tool("search_code", {"query": "foo"}))
                assert search["file_count"] == 1
                assert search["files"][0]["file"] == "src/handler.go"
                assert search["files"][0]["repo"] == "acme/widgets"
                assert search["files"][0]["branches"] == ["HEAD"]
                (m,) = search["files"][0]["matches"]
                assert m["line"] == 3
                for start, end in m["byte_ranges"]:
                    assert m["text"].encode("utf-8")[start:end] == b"foo"

                # sym: folds into search_code (zoekt parity): a sym:-only query the
                # highlight-driven grep path cannot answer returns symbol definitions.
                sym = _tool_json(await session.call_tool("search_code", {"query": "sym:Handler"}))
                assert sym["file_count"] == 1
                (sm,) = sym["files"][0]["matches"]
                assert sm["line"] == 2
                assert sm["symbols"] == [{"name": "Handler", "kind": "function"}]

                repos = _tool_json(await session.call_tool("list_repos", {}))
                assert repos["count"] == 2
                assert {r["name"] for r in repos["repos"]} == {"acme/widgets", "beta/tools"}

                hit = _tool_json(
                    await session.call_tool(
                        "get_file", {"repo": "acme/widgets", "path": "src/handler.go"}
                    )
                )
                assert hit["found"] is True
                assert "func Handler()" in hit["content"]

                miss = _tool_json(
                    await session.call_tool(
                        "get_file", {"repo": "acme/widgets", "path": "does/not/exist.go"}
                    )
                )
                assert miss["found"] is False
                assert miss["content"] is None

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}

            ready = await client.get("/ready")
            assert ready.status_code == 200
            assert ready.json()["status"] == "ready"


@pytest.mark.e2e
async def test_ready_returns_503_when_protected_table_unreadable(empty_schema: str) -> None:
    # The durable tables do not exist in this schema, so `SELECT 1 FROM repos LIMIT 1` raises
    # and readiness reports 503 (the missing-grant / unreachable-table signal M-A guarantees).
    app = main.create_app()  # fresh session manager per test (see _make_client_factory)
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BASE_URL
        ) as client:
            ready = await client.get("/ready")
            assert ready.status_code == 503
            assert ready.json()["status"] == "unready"
