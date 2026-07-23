"""End-to-end tests for the FastMCP server over streamable HTTP.

Requires a running Postgres with the standard PG* env set (CI's service container, or a local
Postgres for `make test-integration`).

Two seams are load-bearing:

* **Schema visibility.** The fixture seeds a throwaway schema and sets
  ``search_path`` on its *own* admin connection, but the server builds its *own* engine whose
  connections default to ``search_path=public``. So BEFORE the server engine is built we set
  ``os.environ["PGOPTIONS"] = "-c search_path=<schema>,public"`` — libpq applies it to every
  server connection (local psycopg path) — and reset the process-scoped engine singleton
  (``app.main._engine = None``) so the server builds a fresh engine under that PGOPTIONS.
* **DNS-rebinding.** ``FastMCP`` defaults ``host=127.0.0.1`` and auto-enables
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
from app.db.models import Base, File, ReferenceEdge, Repo, RepoBranch, Symbol
from indexer.hashing import content_sha

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
    """Throwaway schema + durable-core DDL + the deterministic grep corpus, PGOPTIONS-visible.

    Multi-branch (0003) additions over the base corpus:

    * ``acme/widgets`` also carries a ``feature/x`` content version of ``src/handler.go`` with
      DIVERGENT content (a different ``content_sha``, so it is a separate row per the
      ``(repo_id, path, content_sha)`` unique constraint) -- exercises ``branch:``/``branch``
      filtering and ``get_file(branch=)`` disambiguation.
    * ``repo_branches`` rows for both ``acme/widgets`` branches (but deliberately NONE for
      ``beta/tools``, which keeps the legacy "no repo_branches row" fallback path covered).
    * ``gamma/nullbranch`` has ``default_branch IS NULL`` with one file on ``branches=["HEAD"]``
      -- the NULL-default ``coalesce(...,'HEAD')`` reachability golden case,
      exercised identically at the query-compiler/semantic/``get_file`` sites elsewhere.
    """
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
        gamma_id = conn.execute(
            insert(Repo).values(name="gamma/nullbranch", default_branch=None).returning(Repo.id)
        ).scalar_one()

        handler_content = "package main\nfunc Handler() {}\n// foo lives here and foo again\n"
        handler_file_id = conn.execute(
            insert(File)
            .values(
                repo_id=acme_id,
                path="src/handler.go",
                lang="go",
                content=handler_content,
                content_sha=content_sha(handler_content),
                branches=["main"],
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

        # A DIVERGENT content version of the SAME path on a non-default branch.
        feature_content = "package main\nfunc Handler() {}\n// updated on feature/x\n"
        conn.execute(
            insert(File).values(
                repo_id=acme_id,
                path="src/handler.go",
                lang="go",
                content=feature_content,
                content_sha=content_sha(feature_content),
                branches=["feature/x"],
            )
        )

        conn.execute(
            insert(RepoBranch).values(repo_id=acme_id, branch="main", last_indexed_commit="abc123")
        )
        conn.execute(
            insert(RepoBranch).values(
                repo_id=acme_id, branch="feature/x", last_indexed_commit="feat456"
            )
        )

        gamma_content = "print('head only')\n"
        conn.execute(
            insert(File).values(
                repo_id=gamma_id,
                path="main.py",
                lang="python",
                content=gamma_content,
                content_sha=content_sha(gamma_content),
                branches=["HEAD"],
            )
        )
        conn.execute(insert(RepoBranch).values(repo_id=gamma_id, branch="HEAD"))

        # Reference-edge corpus for find_references / list_imports (D5). Binding content rule:
        # every new file's path AND content avoid "foo" and "Handler" (content idiom
        # "# <path>\n"), all lang="python", no new repos, nothing on feature/x contains
        # "Handler" -- so none of the pre-existing pinned grep/sym/list_repos assertions move.
        def _ref_file(path: str, branch: str) -> int:
            body = f"# {path}\n"
            return conn.execute(
                insert(File)
                .values(
                    repo_id=acme_id,
                    path=path,
                    lang="python",
                    content=body,
                    content_sha=content_sha(body),
                    branches=[branch],
                )
                .returning(File.id)
            ).scalar_one()

        # Two ambiguous "process" definitions on main -> every call site resolves 2 candidates.
        service_py = _ref_file("src/service.py", "main")
        conn.execute(
            insert(Symbol).values(
                file_id=service_py, repo_id=acme_id, name="process", kind="function", start_line=10
            )
        )
        worker_py = _ref_file("src/worker.py", "main")
        conn.execute(
            insert(Symbol).values(
                file_id=worker_py, repo_id=acme_id, name="process", kind="function", start_line=4
            )
        )

        # Call sites on main: one to the ambiguous "process", one to an undefined "missing_fn".
        caller_py = _ref_file("src/caller.py", "main")
        conn.execute(
            insert(ReferenceEdge).values(
                file_id=caller_py,
                repo_id=acme_id,
                edge_kind="call",
                target_name="process",
                line=5,
                enclosing_name="run",
                enclosing_kind="function",
            )
        )
        conn.execute(
            insert(ReferenceEdge).values(
                file_id=caller_py,
                repo_id=acme_id,
                edge_kind="call",
                target_name="missing_fn",
                line=6,
                enclosing_name="run",
                enclosing_kind="function",
            )
        )

        # AC3 composition site: a test file whose enclosing symbol names the covering test.
        test_py = _ref_file("tests/test_service.py", "main")
        conn.execute(
            insert(ReferenceEdge).values(
                file_id=test_py,
                repo_id=acme_id,
                edge_kind="call",
                target_name="process",
                line=8,
                enclosing_name="test_process",
                enclosing_kind="function",
            )
        )

        # Import sites on main (external, module-scope): enclosing NULL, expected "unresolved".
        importer_py = _ref_file("src/importer.py", "main")
        conn.execute(
            insert(ReferenceEdge).values(
                file_id=importer_py,
                repo_id=acme_id,
                edge_kind="import",
                target_name="os.path",
                line=1,
            )
        )
        conn.execute(
            insert(ReferenceEdge).values(
                file_id=importer_py,
                repo_id=acme_id,
                edge_kind="import",
                target_name="collections.abc",
                line=2,
            )
        )

        # Branch parity: a call to "process" on feature/x, where NO "process" def exists ->
        # candidate-side branch scoping makes it resolve "unresolved".
        feature_caller = _ref_file("src/feature_caller.py", "feature/x")
        conn.execute(
            insert(ReferenceEdge).values(
                file_id=feature_caller,
                repo_id=acme_id,
                edge_kind="call",
                target_name="process",
                line=3,
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
                assert {"search_code", "semantic_search", "list_repos", "get_file"} <= names

                # semantic_search is registered UNCONDITIONALLY; with the flag off (the default)
                # it returns the clean feature-absent payload rather than 500/503.
                sem = _tool_json(await session.call_tool("semantic_search", {"query": "auth flow"}))
                assert sem["semantic_enabled"] is False
                assert sem["results"] == []
                assert sem["count"] == 0

                search = _tool_json(await session.call_tool("search_code", {"query": "foo"}))
                # Default-branch scoping (0003): "foo" is only in the "main" content version,
                # so the feature/x divergent version never surfaces without an explicit branch.
                assert search["file_count"] == 1
                assert search["files"][0]["file"] == "src/handler.go"
                assert search["files"][0]["repo"] == "acme/widgets"
                assert search["files"][0]["branches"] == ["main"]  # real membership, not "HEAD"
                (m,) = search["files"][0]["matches"]
                assert m["line"] == 3
                for start, end in m["byte_ranges"]:
                    assert m["text"].encode("utf-8")[start:end] == b"foo"

                # The query-shape keys survive json.dumps in _dispatch and reach the
                # wire; a content query proves neither condition.
                assert search["no_content_atom"] is False
                assert search["zero_width_only_atoms"] is False

                # A filter-only query over the real server: zero files, announced by name
                # rather than returned as a silent empty result.
                filter_only = _tool_json(
                    await session.call_tool("search_code", {"query": "lang:go"})
                )
                assert filter_only["file_count"] == 0
                assert filter_only["no_content_atom"] is True
                assert filter_only["zero_width_only_atoms"] is False

                # sym: folds into search_code (zoekt parity): a sym:-only query the
                # highlight-driven grep path cannot answer returns symbol definitions.
                sym = _tool_json(await session.call_tool("search_code", {"query": "sym:Handler"}))
                assert sym["file_count"] == 1
                (sm,) = sym["files"][0]["matches"]
                assert sm["line"] == 2
                assert sm["symbols"] == [{"name": "Handler", "kind": "function"}]

                # A fully-negated query (issue #70): "-foo" is no_content_atom=True over the
                # real server exactly like a filter-only query -- there is nothing to
                # highlight, and the exclusion never resurrects a file as a false positive.
                negated_only = _tool_json(await session.call_tool("search_code", {"query": "-foo"}))
                assert negated_only["file_count"] == 0
                assert negated_only["no_content_atom"] is True
                assert negated_only["zero_width_only_atoms"] is False

                # `-sym:` alongside a real `sym:` atom that excludes nothing: the affirmative
                # symbol still projects normally end to end (negation on sym: does not
                # silently swallow the positive leg when the excluded name has no matches).
                sym_with_noop_exclusion = _tool_json(
                    await session.call_tool(
                        "search_code", {"query": "sym:Handler -sym:NoSuchSymbol"}
                    )
                )
                assert sym_with_noop_exclusion["file_count"] == 1
                (sm2,) = sym_with_noop_exclusion["files"][0]["matches"]
                assert sm2["symbols"] == [{"name": "Handler", "kind": "function"}]

                repos = _tool_json(await session.call_tool("list_repos", {}))
                assert repos["count"] == 3
                assert {r["name"] for r in repos["repos"]} == {
                    "acme/widgets",
                    "beta/tools",
                    "gamma/nullbranch",
                }
                acme_repo = next(r for r in repos["repos"] if r["name"] == "acme/widgets")
                # list_repos now reflects repo_branches (0003): real per-branch enumeration,
                # not a guess from the single default_branch stamp.
                assert set(acme_repo["branches"]) == {"main", "feature/x"}
                assert acme_repo["default_branch"] == "main"
                assert acme_repo["last_indexed_commit"] == "abc123"  # the DEFAULT branch's stamp
                # beta/tools has NO repo_branches rows (legacy-fallback coverage).
                beta_repo = next(r for r in repos["repos"] if r["name"] == "beta/tools")
                assert beta_repo["branches"] == ["HEAD"]

                hit = _tool_json(
                    await session.call_tool(
                        "get_file", {"repo": "acme/widgets", "path": "src/handler.go"}
                    )
                )
                assert hit["found"] is True
                assert "func Handler()" in hit["content"]
                # Default resolution now returns the REAL branch, not the hardcoded "HEAD".
                assert hit["branch"] == "main"
                assert "foo" in hit["content"]

                miss = _tool_json(
                    await session.call_tool(
                        "get_file", {"repo": "acme/widgets", "path": "does/not/exist.go"}
                    )
                )
                assert miss["found"] is False
                assert miss["content"] is None

                # branch: filtering (0003) -- both the `branch` param and the raw `branch:`
                # query atom must surface the feature/x-only content version.
                feature_search = _tool_json(
                    await session.call_tool(
                        "search_code", {"query": "Handler", "branch": "feature/x"}
                    )
                )
                assert feature_search["file_count"] == 1
                assert feature_search["files"][0]["branches"] == ["feature/x"]

                feature_search_atom = _tool_json(
                    await session.call_tool("search_code", {"query": 'Handler branch:"feature/x"'})
                )
                assert feature_search_atom["file_count"] == 1
                assert feature_search_atom["files"][0]["branches"] == ["feature/x"]
                # permalink_branch: a query with an explicit branch: atom resolves
                # the emitted file's permalink to that same branch.
                assert feature_search_atom["files"][0]["permalink_branch"] == "feature/x"

                # get_file(branch=) disambiguates the two divergent content versions of the
                # SAME path -- proves the predicate stays single-row (scalar_one_or_none never
                # raises MultipleResultsFound) even though `files` now has two rows for this path.
                feature_file = _tool_json(
                    await session.call_tool(
                        "get_file",
                        {
                            "repo": "acme/widgets",
                            "path": "src/handler.go",
                            "branch": "feature/x",
                        },
                    )
                )
                assert feature_file["found"] is True
                assert feature_file["branch"] == "feature/x"
                assert "updated on feature/x" in feature_file["content"]
                assert "foo" not in feature_file["content"]

                # NULL default_branch reachability: coalesce(...,'HEAD')
                # resolves the same way here as at the compiler/semantic/backfill sites.
                null_default_file = _tool_json(
                    await session.call_tool(
                        "get_file", {"repo": "gamma/nullbranch", "path": "main.py"}
                    )
                )
                assert null_default_file["found"] is True
                assert null_default_file["branch"] == "HEAD"
                assert "head only" in null_default_file["content"]

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
async def test_reference_tools_streamable_http(seeded_schema: str) -> None:
    app = main.create_app()  # fresh session manager per test (see _make_client_factory)
    async with LifespanManager(app):
        async with streamablehttp_client(
            MCP_URL, httpx_client_factory=_make_client_factory(app)
        ) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()

                # AC1 registration: both reference tools are exposed over the wire.
                names = {t.name for t in (await session.list_tools()).tools}
                assert {"find_references", "list_imports"} <= names

                # find_references("process"): two ambiguous call sites (caller.py, tests/), each
                # with 2 ranked candidates; the feature/x call site is NOT in default-branch scope.
                refs = _tool_json(await session.call_tool("find_references", {"symbol": "process"}))
                assert refs["site_count"] == 2
                assert refs["resolution_summary"] == {"unique": 0, "ambiguous": 2, "unresolved": 0}
                for site in refs["sites"]:
                    assert site["resolution"] == "ambiguous"
                    assert len(site["candidates"]) == 2
                assert "repo_known" not in refs  # pinned absent on find_references (Critic note 5)

                # AC3: "what tests cover process" = client-side test-path filter of the sites.
                test_sites = [s for s in refs["sites"] if s["file"].startswith("tests/")]
                assert len(test_sites) == 1
                assert test_sites[0]["enclosing_symbol"] == {
                    "name": "test_process",
                    "kind": "function",
                }

                # An undefined callee resolves to a single unresolved site, zero candidates.
                missing = _tool_json(
                    await session.call_tool("find_references", {"symbol": "missing_fn"})
                )
                assert missing["site_count"] == 1
                (missing_site,) = missing["sites"]
                assert missing_site["resolution"] == "unresolved"
                assert missing_site["candidate_count"] == 0

                # Branch parity: on feature/x the only "process" call has no candidate def there.
                feature_refs = _tool_json(
                    await session.call_tool(
                        "find_references", {"symbol": "process", "branch": "feature/x"}
                    )
                )
                assert feature_refs["site_count"] == 1
                (feature_site,) = feature_refs["sites"]
                assert feature_site["file"] == "src/feature_caller.py"
                assert feature_site["resolution"] == "unresolved"

                # list_imports(imports): enumerate a repo's external import sites.
                imports = _tool_json(
                    await session.call_tool("list_imports", {"repo": "acme/widgets"})
                )
                assert imports["repo_known"] is True
                assert imports["direction"] == "imports"
                assert {s["target_name"] for s in imports["sites"]} == {
                    "os.path",
                    "collections.abc",
                }
                for site in imports["sites"]:
                    assert site["resolution"] == "unresolved"
                    assert site["enclosing_symbol"] is None

                # list_imports(imported_by): who imports os.path, corpus-wide (no repo scope).
                imported_by = _tool_json(
                    await session.call_tool(
                        "list_imports", {"target": "os.path", "direction": "imported_by"}
                    )
                )
                assert imported_by["repo"] is None
                assert imported_by["repo_known"] is True
                assert imported_by["site_count"] == 1
                (ib_site,) = imported_by["sites"]
                assert ib_site["file"] == "src/importer.py"
                assert ib_site["line"] == 1

                # Structured validation over the wire (D5: >= 1 validation case at e2e level).
                bad_direction = _tool_json(
                    await session.call_tool(
                        "list_imports", {"repo": "acme/widgets", "direction": "sideways"}
                    )
                )
                assert bad_direction["unsupported_direction"] == "sideways"
                assert bad_direction["sites"] == []
                assert bad_direction["site_count"] == 0

                no_repo = _tool_json(await session.call_tool("list_imports", {}))
                assert no_repo["missing_repo"] is True
                assert no_repo["sites"] == []

                no_target = _tool_json(
                    await session.call_tool("list_imports", {"direction": "imported_by"})
                )
                assert no_target["missing_target"] is True
                assert no_target["sites"] == []


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
