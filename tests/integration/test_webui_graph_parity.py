"""Integration test proving AC2 for issue #88: the webui ``/api/references``/``/api/imports``
routes serve payloads byte-identical to ``app.service``'s builders -- the SAME builders the MCP
``find_references``/``list_imports`` tools wrap -- over a real seeded reference-edge corpus.

**Transitivity, not a second live harness.** ``app/main.py``'s ``find_references``/
``list_imports`` MCP tools are pure ``clamp_limit`` -> ``json.dumps(builder(...))`` wrappers
around these SAME ``app.service`` builder functions (see ``app/main.py``'s module docstring),
independently pinned wire-shape-identical to the builder output by
``tests/integration/test_mcp_server.py::test_reference_tools_streamable_http``. So proving
``webui route JSON == builder output`` here (over the identical corpus, at the identical
clamped limit) transitively proves ``webui route JSON == MCP wire payload`` without a second
live MCP client/server round trip in this test (rejected in the binding plan: wiring
cost/flakiness, no added guarantee over the existing MCP e2e pin).

**Seed reuse.** ``seed_reference_corpus`` is the SAME connection-parameterized helper
``tests/integration/test_mcp_server.py::seeded_schema`` uses (extracted there for reuse here);
this fixture builds its own throwaway schema/repo and calls it directly, rather than duplicating
the corpus.

**Clamp discipline (Critic note 4).** Every direct-builder comparison call below applies
``service.clamp_limit(request_limit, cfg)`` to its ``limit`` argument, exactly mirroring what
the route does -- an un-clamped direct call would make a "byte-identical" assertion pass
vacuously on this small corpus even if the route's own clamping regressed.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from app import service
from app.config import Settings
from app.db.client import create_db_engine
from app.db.models import Base, Repo
from tests.integration.test_mcp_server import seed_reference_corpus
from webui.main import app, get_engine, get_settings

SCHEMA_PREFIX = "test_webui_graph_parity"


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _cfg() -> Settings:
    return Settings(
        lakebase_endpoint=None,
        statement_timeout_ms=5000,
        row_limit=200,
        max_row_limit=1000,
        semantic_enabled=False,
    )


def _set_pgoptions(schema: str) -> str | None:
    """Point every connection THIS engine opens at ``schema`` via libpq PGOPTIONS.

    Mirrors ``tests/integration/test_webui_semantic.py``: set BEFORE building the engine so
    every pooled connection is born under the right ``search_path`` (a bare
    ``SET search_path`` on one connection would not follow the pool).
    """
    prev = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = f"-c search_path={schema},public"
    return prev


def _restore_pgoptions(prev: str | None) -> None:
    if prev is None:
        os.environ.pop("PGOPTIONS", None)
    else:
        os.environ["PGOPTIONS"] = prev


@pytest.fixture
def reference_engine() -> Iterator[Engine]:
    """A throwaway schema + durable-core DDL + the shared reference-edge corpus, on its own
    engine (webui routes take their engine via ``dependency_overrides``, not a process-scoped
    singleton built from env -- so this fixture owns its engine directly, unlike
    ``test_mcp_server.py``'s ``seeded_schema``)."""
    schema = _unique(SCHEMA_PREFIX)
    prev_pgoptions = _set_pgoptions(schema)
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
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
            seed_reference_corpus(conn, acme_id)
            conn.commit()
        yield engine
    finally:
        with engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            conn.commit()
        engine.dispose()
        _restore_pgoptions(prev_pgoptions)


@pytest.fixture
def client(reference_engine: Engine) -> Iterator[TestClient]:
    app.dependency_overrides[get_engine] = lambda: reference_engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def _direct_references(
    engine: Engine, name: str, limit: int, branch: str | None = None
) -> dict[str, Any]:
    cfg = _cfg()
    clamped = service.clamp_limit(limit, cfg)
    return service.find_references_payload(engine, cfg, name, clamped, branch)


def _direct_imports(
    engine: Engine,
    repo: str | None = None,
    limit: int = 200,
    branch: str | None = None,
    *,
    target: str | None = None,
    direction: str = "imports",
) -> dict[str, Any]:
    cfg = _cfg()
    clamped = service.clamp_limit(limit, cfg)
    return service.list_imports_payload(
        engine, cfg, repo, clamped, branch, target=target, direction=direction
    )


@pytest.mark.integration
def test_find_references_ambiguous_process_parity(
    client: TestClient, reference_engine: Engine
) -> None:
    resp = client.get("/api/references", params={"symbol": "process"})
    assert resp.status_code == 200
    body = resp.json()

    expected = _direct_references(reference_engine, "process", 200)
    assert body == expected

    # Corpus shape sanity (D5 seed): two ambiguous 2-candidate call sites (src/caller.py,
    # tests/test_service.py), never collapsed to one answer.
    assert body["resolution_summary"] == {"unique": 0, "ambiguous": 2, "unresolved": 0}
    assert body["site_count"] == 2
    for site in body["sites"]:
        assert site["resolution"] == "ambiguous"
        assert site["candidate_count"] == 2


@pytest.mark.integration
def test_find_references_branch_scoped_unresolved_caller_parity(
    client: TestClient, reference_engine: Engine
) -> None:
    resp = client.get("/api/references", params={"symbol": "process", "branch": "feature/x"})
    assert resp.status_code == 200
    body = resp.json()

    expected = _direct_references(reference_engine, "process", 200, branch="feature/x")
    assert body == expected

    # feature/x has a caller of "process" but no "process" definition on that branch ->
    # candidate-side branch scoping resolves it unresolved.
    assert body["site_count"] == 1
    assert body["sites"][0]["resolution"] == "unresolved"
    assert body["sites"][0]["candidate_count"] == 0


@pytest.mark.integration
def test_find_references_undefined_symbol_parity(
    client: TestClient, reference_engine: Engine
) -> None:
    resp = client.get("/api/references", params={"symbol": "missing_fn"})
    assert resp.status_code == 200
    body = resp.json()

    expected = _direct_references(reference_engine, "missing_fn", 200)
    assert body == expected

    assert body["site_count"] == 1
    assert body["sites"][0]["resolution"] == "unresolved"
    assert body["sites"][0]["candidate_count"] == 0


@pytest.mark.integration
def test_list_imports_by_repo_parity(client: TestClient, reference_engine: Engine) -> None:
    resp = client.get("/api/imports", params={"repo": "acme/widgets"})
    assert resp.status_code == 200
    body = resp.json()

    expected = _direct_imports(reference_engine, repo="acme/widgets")
    assert body == expected

    assert body["repo_known"] is True
    assert body["direction"] == "imports"
    targets = {site["target_name"] for site in body["sites"]}
    assert targets == {"os.path", "collections.abc"}
    # Import edges are module-scope (enclosing_symbol None) and external-by-design ->
    # "unresolved" is expected, not an error.
    for site in body["sites"]:
        assert site["enclosing_symbol"] is None
        assert site["resolution"] == "unresolved"


@pytest.mark.integration
def test_list_imports_imported_by_target_parity(
    client: TestClient, reference_engine: Engine
) -> None:
    resp = client.get("/api/imports", params={"target": "os.path", "direction": "imported_by"})
    assert resp.status_code == 200
    body = resp.json()

    expected = _direct_imports(reference_engine, target="os.path", direction="imported_by")
    assert body == expected

    assert body["repo"] is None
    assert body["direction"] == "imported_by"
    assert body["site_count"] == 1
    assert body["sites"][0]["target_name"] == "os.path"


@pytest.mark.integration
def test_list_imports_unsupported_direction_is_200_structured(
    client: TestClient, reference_engine: Engine
) -> None:
    resp = client.get("/api/imports", params={"direction": "sideways"})
    assert resp.status_code == 200
    body = resp.json()

    expected = _direct_imports(reference_engine, direction="sideways")
    assert body == expected

    assert body["unsupported_direction"] == "sideways"
    assert "reason" in body
    assert body["sites"] == []
    assert body["site_count"] == 0
