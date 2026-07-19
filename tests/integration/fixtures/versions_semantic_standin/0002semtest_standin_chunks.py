"""TEST-ONLY stand-in for the gated semantic revision.

Mirrors the REAL ``app/alembic/versions_semantic/0002sem_semantic_chunks.py`` shape
(standalone branch head: ``down_revision=None``, ``branch_labels=("semantic",)``, NO
``depends_on``) so the C1 isolation regression test can record a semantic head into a
SEPARATE version table without needing the beta ``lakebase_*`` extensions, which the CI
``pgvector/pgvector:pg16`` image lacks. It builds a pgvector stand-in ``chunks`` table
(``vector`` + a plain generated ``tsvector`` + hnsw/gin indexes) rather than the
production ``lakebase_ann``/``lakebase_bm25`` surface. It is unreachable from any
production import; it exists only so a test can drive ``upgrade semantic@head``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.config import SEMANTIC_EMBEDDING_DIM

# A distinct revision id from the real "0002sem" so the two can never be confused; the
# semantic BRANCH LABEL is what the test targets via "semantic@head".
revision: str = "0002semtest"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = ("semantic",)
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "CREATE TABLE chunks ("
        "id bigserial PRIMARY KEY, "
        "file_id integer NOT NULL REFERENCES files(id) ON DELETE CASCADE, "
        "chunk_index integer NOT NULL, "
        "content text NOT NULL, "
        f"embedding vector({SEMANTIC_EMBEDDING_DIM}), "
        "ts tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED, "
        # Mirrors the real revision: supplies the file_id-leading index that
        # chunk_store's per-file DELETE and the sweep's ON DELETE CASCADE need.
        "CONSTRAINT uq_chunks_file_id_chunk_index UNIQUE (file_id, chunk_index))"
    )
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX ix_chunks_ts_gin ON chunks USING gin (ts)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_ts_gin")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
    op.execute("DROP TABLE IF EXISTS chunks")
