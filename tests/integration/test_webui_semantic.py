"""Integration tests for the webui ``/api/semantic`` route against a Lakebase branch (issue #36).

Runs the production ``lakebase_ann``/``lakebase_bm25`` operators (the suite's Lakebase
branch preloads them -- see docs/runbooks/ci-lakebase.md). It proves the webui **route**
(dependency wiring, HTTP status/shape, the not-migrated/disabled payload passthrough), not the
production ranking -- that is ``tests/integration/test_semantic_rrf.py``'s job.

Two seams carried over from sibling integration suites:

* **Embedder seam** (``tests/unit/test_semantic.py:240``): ``app.search.semantic.get_embedder``
  is monkeypatched to a fake that returns ``SEMANTIC_EMBEDDING_DIM``-wide vectors (reusing
  ``test_semantic_rrf.py``'s ``_vec()`` helper) -- short vectors fail against the real
  ``vector(1024)`` column.
* **search_path trap** (``tests/integration/test_mcp_server.py:9-11,65``): the route's service
  call opens its OWN connections (``search_path=public`` by default), so
  ``os.environ["PGOPTIONS"] = "-c search_path=<schema>,public"`` is set BEFORE building the
  engine handed to ``dependency_overrides``, and each fixture immediately opens/seeds its
  schema through that same engine so its pooled connection(s) are warmed under the correct
  ``PGOPTIONS`` before any other fixture changes the process-global env var again.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from app.config import SEMANTIC_EMBEDDING_DIM, Settings
from app.db.client import create_db_engine
from app.db.models import Base, File, Repo
from app.search import semantic as semantic_module
from indexer.chunk_store import write_chunks
from indexer.hashing import content_sha
from webui.main import app, get_engine, get_settings

SCHEMA_PREFIX = "test_webui_semantic"


def _vec(pairs: dict[int, float]) -> list[float]:
    """A ``SEMANTIC_EMBEDDING_DIM``-wide vector with ``pairs`` (index -> value) set, else 0."""
    v = [0.0] * SEMANTIC_EMBEDDING_DIM
    for i, x in pairs.items():
        v[i] = x
    return v


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _enabled_cfg() -> Settings:
    return Settings(
        lakebase_endpoint=None,
        statement_timeout_ms=5000,
        row_limit=200,
        max_row_limit=1000,
        semantic_enabled=True,
    )


def _set_pgoptions(schema: str) -> str | None:
    """Point every connection opened AFTER this call at ``schema``; return the prior value."""
    prev = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = f"-c search_path={schema},public"
    return prev


def _restore_pgoptions(prev: str | None) -> None:
    if prev is None:
        os.environ.pop("PGOPTIONS", None)
    else:
        os.environ["PGOPTIONS"] = prev


@pytest.fixture
def semantic_engine() -> Iterator[Engine]:
    """A throwaway schema with core DDL + the real (0004-shaped) ``chunks``, seeded with two
    embedded chunks -- one aligned with the query vector used below, one not.

    Mirrors ``test_semantic_rrf.py``'s ``seeded`` fixture's DDL, but the schema/DDL/seed all
    run through THIS fixture's own engine (built right after ``PGOPTIONS`` is set) so its
    pooled connection(s) are warmed with the correct ``search_path`` before any other fixture
    in the same test flips the process-global ``PGOPTIONS`` again.
    """
    schema = _unique(SCHEMA_PREFIX)
    prev_pgoptions = _set_pgoptions(schema)
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS lakebase_tokenizer CASCADE"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE"))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS lakebase_text CASCADE"))
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {schema}"))
            conn.execute(text(f"SET search_path TO {schema}, public"))
            conn.commit()

            Base.metadata.create_all(bind=conn)
            conn.execute(
                text(
                    "CREATE TABLE chunks ("
                    "id bigserial PRIMARY KEY, "
                    "file_id integer NOT NULL REFERENCES files(id) ON DELETE CASCADE, "
                    "chunk_index integer NOT NULL, "
                    "content text NOT NULL, "
                    "start_line integer, "
                    "end_line integer, "
                    f"embedding vector({SEMANTIC_EMBEDDING_DIM}), "
                    "ts tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED, "
                    "CONSTRAINT uq_chunks_file_id_chunk_index UNIQUE (file_id, chunk_index))"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX ix_chunks_embedding_ann ON chunks "
                    "USING lakebase_ann (embedding vector_cosine_ops)"
                )
            )
            conn.execute(text("CREATE INDEX ix_chunks_ts_bm25 ON chunks USING lakebase_bm25 (ts)"))
            conn.commit()

            repo_id = conn.execute(
                insert(Repo).values(name="acme/widgets", default_branch="main").returning(Repo.id)
            ).scalar_one()
            file_id = conn.execute(
                insert(File)
                .values(
                    repo_id=repo_id,
                    path="src/auth.py",
                    lang="python",
                    content="stub",
                    content_sha=content_sha("stub"),
                    branches=["main"],
                )
                .returning(File.id)
            ).scalar_one()
            write_chunks(
                conn,
                file_id=file_id,
                chunks=[
                    (0, "user authentication and login", 1, 1, _vec({0: 1.0})),
                    (1, "database connection pooling and retries", 2, 2, _vec({0: 0.7, 1: 0.7})),
                ],
            )
            conn.commit()

        yield engine
    finally:
        with engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            conn.commit()
        engine.dispose()
        _restore_pgoptions(prev_pgoptions)


@pytest.fixture
def not_migrated_engine() -> Iterator[Engine]:
    """A bare throwaway schema with NO ``chunks`` table.

    ``to_regclass('chunks')`` resolves to NULL under this engine's ``PGOPTIONS``-scoped
    ``search_path`` (and ``public`` never carries a ``chunks`` table either, since every other
    fixture keeps its own DDL inside its own throwaway schema), so the route falls into the
    not-migrated payload -- the A2 pattern's "falls out for free" case.
    """
    schema = _unique(SCHEMA_PREFIX + "_nomig")
    prev_pgoptions = _set_pgoptions(schema)
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {schema}"))
            conn.commit()
        yield engine
    finally:
        with engine.connect() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            conn.commit()
        engine.dispose()
        _restore_pgoptions(prev_pgoptions)


@pytest.mark.integration
def test_api_semantic_enabled_returns_deterministic_ordered_results(
    semantic_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake embedder always returns a vector aligned with the "authentication" chunk,
    # regardless of the query text -- the RRF fusion/ordering behavior is what's under test,
    # not embedding fidelity (that is out of scope for this stand-in).
    monkeypatch.setattr(
        semantic_module, "get_embedder", lambda cfg: lambda texts: [_vec({0: 1.0})] * len(texts)
    )
    app.dependency_overrides[get_engine] = lambda: semantic_engine
    app.dependency_overrides[get_settings] = _enabled_cfg
    try:
        with TestClient(app) as client:
            resp1 = client.get("/api/semantic", params={"q": "authentication"})
            resp2 = client.get("/api/semantic", params={"q": "authentication"})
    finally:
        app.dependency_overrides.clear()

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    body1 = resp1.json()
    body2 = resp2.json()

    assert body1["semantic_enabled"] is True
    assert body1["results"] != []
    assert body1["count"] == len(body1["results"])

    # Determinism: identical requests return the identically-ordered result set.
    assert body1["results"] == body2["results"]

    scores = [r["rrf_score"] for r in body1["results"]]
    assert all(isinstance(s, float) for s in scores)
    assert scores == sorted(scores, reverse=True)

    # Line ranges round-trip to the HTTP envelope (issue #44).
    top = body1["results"][0]
    assert (top["start_line"], top["end_line"]) == (1, 1)


@pytest.mark.integration
def test_api_semantic_not_migrated_returns_schema_missing_payload(
    not_migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _never(_cfg: Settings) -> None:
        raise AssertionError("embedder must not be built when the schema is absent")

    monkeypatch.setattr(semantic_module, "get_embedder", _never)
    app.dependency_overrides[get_engine] = lambda: not_migrated_engine
    app.dependency_overrides[get_settings] = _enabled_cfg
    try:
        with TestClient(app) as client:
            resp = client.get("/api/semantic", params={"q": "authentication"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["semantic_enabled"] is True
    assert body["semantic_schema_missing"] is True
    assert body["results"] == []


@pytest.mark.integration
def test_api_semantic_status_true_when_enabled_without_engine_override() -> None:
    # No get_engine override at all: /api/semantic/status must never touch the DB.
    app.dependency_overrides[get_settings] = _enabled_cfg
    try:
        with TestClient(app) as client:
            resp = client.get("/api/semantic/status")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {"semantic_enabled": True}
