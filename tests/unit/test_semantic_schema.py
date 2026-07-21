"""Isolation + dim tripwires for the semantic (issue #14 Phase 0) chunks schema.

These guard the two invariants Phase 0 exists to establish:

* ``chunks`` never leaks into ``app.db.models.Base.metadata`` -- the core migration
  graph must never autogenerate against a beta-extension-backed table (see
  ``app/db/semantic.py``'s module docstring).
* the embedding dimension is single-sourced from ``app.config.SEMANTIC_EMBEDDING_DIM``,
  so a model swap with a different dim fails at test time, not at first DB write.
"""

from __future__ import annotations

import pytest
from pgvector.sqlalchemy import Vector

from app.config import SEMANTIC_EMBEDDING_DIM, Settings, get_settings
from app.db.models import Base
from app.db.semantic import chunks, semantic_metadata


@pytest.mark.unit
def test_chunks_not_in_core_metadata() -> None:
    assert "chunks" not in Base.metadata.tables


@pytest.mark.unit
def test_chunks_in_semantic_metadata() -> None:
    assert "chunks" in semantic_metadata.tables
    assert semantic_metadata is not Base.metadata


@pytest.mark.unit
def test_embedding_dim_single_sourced() -> None:
    assert SEMANTIC_EMBEDDING_DIM == 1024
    assert Settings().semantic_embedding_dim == SEMANTIC_EMBEDDING_DIM


@pytest.mark.unit
def test_semantic_defaults_are_on_and_gateway_backed() -> None:
    """Default-on contract (AC5): no env needed on any app/job for semantic search."""
    cfg = get_settings()
    assert cfg.semantic_enabled is True
    assert cfg.semantic_embedding_model == "system.ai.gte-large-en"
    assert cfg.semantic_embedding_endpoint == "/ai-gateway/mlflow/v1/embeddings"


@pytest.mark.unit
def test_semantic_opt_out_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_SEARCH_SEMANTIC_ENABLED", "0")
    assert Settings().semantic_enabled is False


@pytest.mark.unit
def test_chunks_columns_and_embedding_dim() -> None:
    expected = {
        "id",
        "file_id",
        "chunk_index",
        "content",
        "start_line",
        "end_line",
        "embedding",
        "ts",
    }
    assert set(chunks.columns.keys()) == expected
    assert chunks.c.id.primary_key
    assert not chunks.c.file_id.nullable
    assert not chunks.c.chunk_index.nullable
    assert not chunks.c.content.nullable
    embedding_type = chunks.c.embedding.type
    assert isinstance(embedding_type, Vector)
    assert embedding_type.dim == SEMANTIC_EMBEDDING_DIM
