"""Static source assertions on the GATED semantic revision (no database required).

The real ``0002sem`` DDL can never run in CI -- the beta ``lakebase_*`` extensions
are absent from ``pgvector/pgvector:pg16`` -- so a static read of the source is the
ONLY automated guard on its hand-written invariants. Each assertion below encodes a
decision that was verified against the live beta database and would be silently
wrong if edited away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import SEMANTIC_EMBEDDING_DIM

_SEMANTIC_VERSIONS = Path(__file__).resolve().parents[2] / "app" / "alembic" / "versions_semantic"


@pytest.fixture
def source() -> str:
    matches = sorted(_SEMANTIC_VERSIONS.glob("0002sem*.py"))
    assert len(matches) == 1, f"expected exactly one 0002sem*.py, found {matches}"
    return matches[0].read_text()


@pytest.mark.unit
def test_branch_is_self_rooted_and_labelled(source: str) -> None:
    """down_revision=None + branch_labels, and NO depends_on.

    A depends_on edge to 0001 would try to re-run 0001 into the separate
    alembic_version_semantic table (which cannot see it as applied).
    """
    assert 'revision: str = "0002sem"' in source
    assert "down_revision: str | None = None" in source
    assert 'branch_labels: str | Sequence[str] | None = ("semantic",)' in source
    assert "depends_on: str | Sequence[str] | None = None" in source


@pytest.mark.unit
def test_extensions_are_created_before_dependent_indexes(source: str) -> None:
    order = [
        source.index("CREATE EXTENSION IF NOT EXISTS lakebase_tokenizer"),
        source.index("CREATE EXTENSION IF NOT EXISTS lakebase_vector"),
        source.index("CREATE EXTENSION IF NOT EXISTS lakebase_text"),
        source.index("CREATE TABLE chunks"),
        source.index("USING lakebase_ann"),
        source.index("USING lakebase_bm25"),
    ]
    assert order == sorted(order), "extensions must precede the table and its indexes"


@pytest.mark.unit
def test_ann_index_uses_lakebase_ann_with_explicit_opclass(source: str) -> None:
    """vector_cosine_ops is NON-default on lakebase_ann and must be explicit."""
    assert "USING lakebase_ann (embedding vector_cosine_ops)" in source


@pytest.mark.unit
def test_ann_index_passes_no_hnsw_params(source: str) -> None:
    """lakebase_ann REJECTS m/ef_construction ("unrecognized parameter") -- verified live.

    Asserted against the emitted DDL, not the whole file: the surrounding comment
    deliberately NAMES those params to explain why they must not be used.
    """
    # The opclass closes the SQL string literal, so no WITH (...) clause can follow it
    # in the emitted DDL. Checked this way rather than `"ef_construction" not in source`
    # because the adjacent comment names those params on purpose.
    assert 'USING lakebase_ann (embedding vector_cosine_ops)"' in source


@pytest.mark.unit
def test_bm25_index_is_over_the_generated_ts_column(source: str) -> None:
    assert "USING lakebase_bm25 (ts)" in source
    assert "ts tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED" in source


@pytest.mark.unit
def test_chunks_has_a_file_id_leading_unique_constraint(source: str) -> None:
    """The file_id index is a WRITE-path requirement, not just an integrity nicety.

    Postgres does not auto-index a foreign key. chunk_store's per-file DELETE and the
    mark-and-sweep ON DELETE CASCADE both look chunks up by file_id; without a
    file_id-leading index each degrades to a full scan of chunks per file, inside the
    open transaction. CI cannot catch this (stand-in tables hold a handful of rows).
    """
    assert "CONSTRAINT uq_chunks_file_id_chunk_index UNIQUE (file_id, chunk_index)" in source


@pytest.mark.unit
def test_embedding_dim_is_single_sourced_from_config(source: str) -> None:
    """The DDL interpolates the module int constant, not the env-overridable setting."""
    assert "from app.config import SEMANTIC_EMBEDDING_DIM" in source
    assert "embedding vector({SEMANTIC_EMBEDDING_DIM})" in source
    assert SEMANTIC_EMBEDDING_DIM == 1024


@pytest.mark.unit
def test_downgrade_does_not_drop_shared_extensions(source: str) -> None:
    """Extensions are database-wide; dropping them could break other schemas."""
    downgrade = source[source.index("def downgrade()") :]
    assert "DROP TABLE IF EXISTS chunks" in downgrade
    assert "DROP EXTENSION" not in downgrade
