"""Static source assertions on the semantic ``0004`` revision (no database required).

The real ``0004`` DDL only runs against a Lakebase branch (unit CI has no database at
all), so a static read of the source is the fastest automated guard on its hand-written
invariants. Each assertion below encodes a decision that was verified against the live
database and would be silently wrong if edited away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import SEMANTIC_EMBEDDING_DIM

_VERSIONS = Path(__file__).resolve().parents[2] / "app" / "alembic" / "versions"


@pytest.fixture
def source() -> str:
    matches = sorted(_VERSIONS.glob("0004*.py"))
    assert len(matches) == 1, f"expected exactly one 0004*.py, found {matches}"
    return matches[0].read_text()


@pytest.mark.unit
def test_revision_is_core_chained(source: str) -> None:
    """0004 is a plain core revision: chained off 0003, no branch labels, no depends_on."""
    assert 'revision: str = "0004"' in source
    assert 'down_revision: str | None = "0003"' in source
    assert "branch_labels: str | Sequence[str] | None = None" in source
    assert "depends_on: str | Sequence[str] | None = None" in source


@pytest.mark.unit
def test_idempotency_guard_probes_chunks_first(source: str) -> None:
    """A project that already ran the old gated migrate-semantic has chunks; the guard
    must short-circuit the DDL (and clean up the orphaned semantic version table)."""
    upgrade = source[source.index("def upgrade()") : source.index("def downgrade()")]
    guard_pos = upgrade.index("to_regclass('chunks')")
    ddl_pos = upgrade.index("CREATE EXTENSION")
    assert guard_pos < ddl_pos, "the to_regclass('chunks') guard must precede the DDL"
    assert "DROP TABLE IF EXISTS alembic_version_semantic" in upgrade


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
def test_extensions_are_created_with_cascade(source: str) -> None:
    """CASCADE is load-bearing (ground truth 2026-07-21): lakebase_vector declares a
    dependency on the base ``vector`` extension, so a bare CREATE EXTENSION fails with
    'required extension "vector" is not installed' where ``vector`` isn't pre-installed.
    CASCADE does not mask the preload fail-loud signal."""
    for ext in ("lakebase_tokenizer", "lakebase_vector", "lakebase_text"):
        assert f"CREATE EXTENSION IF NOT EXISTS {ext} CASCADE" in source


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
    open transaction.
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
