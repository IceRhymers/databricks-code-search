"""semantic chunks (core chain; default-on semantic search)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-21 12:00:00.000000

Semantic search is enabled by default, so the ``chunks`` surface ships with the
core migration chain: ``make migrate`` (and therefore ``make deploy``) creates it
with no separate operator ceremony. This supersedes the formerly GATED revision
``0002sem`` (``app/alembic/versions_semantic/``, applied via the now-removed
``make migrate-semantic`` into a separate ``alembic_version_semantic`` table).

**Project-level assumption (was a gate, now an assumption):** the target Lakebase
project's Databricks-managed ``shared_preload_libraries`` already includes
``lakebase_vector,lakebase_text``. That preload change is irreversible,
project-level, and performed out-of-band (UI/support) -- this migration cannot
perform it. When the preload is absent, ``CREATE EXTENSION`` fails loudly with
"must be loaded via shared_preload_libraries" -- now at ``make migrate``/deploy
time -- and that error IS the signal to go complete the preload prerequisite.
See ``docs/runbooks/semantic-enablement.md``.

**Idempotency guard:** a project that already ran the old gated migration has
``chunks`` (and possibly ``alembic_version_semantic``). ``to_regclass('chunks')``
short-circuits the CREATE DDL for that case -- this revision then only adds the
``start_line``/``end_line`` columns the gated revision predates (issue #44), records
itself in the core ``alembic_version``, and drops the orphaned semantic version table.

**Line ranges (issue #44):** ``start_line``/``end_line`` (1-based, inclusive) are
NULLABLE -- rows written before the line-aware chunk writer stay NULL until the next
re-index naturally backfills them (the INDEX_SEMANTICS_VERSION bump forces exactly
that), and readers must treat NULL as "no authoritative range" (the webui falls back
to its needle-match anchor).

Ground truth (captured 2026-07-19 against the live ``code-search`` Lakebase project,
Postgres 17, on a disposable branch): the ANN access method is ``lakebase_ann`` with
the non-default ``vector_cosine_ops`` opclass (and it REJECTS any
``WITH (m=..., ef_construction=...)`` params); the BM25 access method is
``lakebase_bm25`` over the generated ``ts`` ``tsvector`` column. ``lakebase_text``
builds on ``lakebase_tokenizer``, so the extensions are created tokenizer -> vector
-> text before the dependent indexes (mirroring 0001's extension-before-index order).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from app.config import SEMANTIC_EMBEDDING_DIM

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotency guard: a project that ran the old gated `migrate-semantic` already has
    # the table (created by the retired 0002sem revision). Skip the CREATE DDL, add only
    # the line-range columns that revision predates (issue #44; IF NOT EXISTS makes a
    # re-run safe), and clean up the orphaned separate version table; this revision
    # still lands in `alembic_version`.
    bind = op.get_bind()
    if bind.execute(text("SELECT to_regclass('chunks')")).scalar() is not None:
        op.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS start_line integer")
        op.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS end_line integer")
        op.execute("DROP TABLE IF EXISTS alembic_version_semantic")
        return

    # Extensions first (tokenizer -> vector -> text), mirroring 0001's ordering so the
    # dependent lakebase_ann / lakebase_bm25 access methods resolve when the indexes
    # below are built. lakebase_text builds on lakebase_tokenizer. These FAIL LOUDLY
    # if the managed shared_preload_libraries prerequisite is absent -- that is the
    # intended signal (see module docstring + the enablement runbook).
    op.execute("CREATE EXTENSION IF NOT EXISTS lakebase_tokenizer")
    op.execute("CREATE EXTENSION IF NOT EXISTS lakebase_vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS lakebase_text")
    # The embedding width is single-sourced from app.config.SEMANTIC_EMBEDDING_DIM so
    # the DDL and the app.db.semantic.chunks Table can never drift apart.
    op.execute(
        "CREATE TABLE chunks ("
        "id bigserial PRIMARY KEY, "
        "file_id integer NOT NULL REFERENCES files(id) ON DELETE CASCADE, "
        "chunk_index integer NOT NULL, "
        "content text NOT NULL, "
        # 1-based inclusive line range of the chunk within its file (issue #44).
        # NULLABLE: rows from a pre-line-aware writer stay NULL until re-indexed,
        # and readers fall back to needle-matching when absent.
        "start_line integer, "
        "end_line integer, "
        f"embedding vector({SEMANTIC_EMBEDDING_DIM}), "
        "ts tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED, "
        # Load-bearing for WRITE performance, not just integrity. Postgres does NOT
        # auto-index a foreign key, and two hot paths look chunks up by file_id:
        # chunk_store's per-file DELETE (once per file, every index run) and the
        # ON DELETE CASCADE fired by store.py's mark-and-sweep. Without a
        # file_id-leading index both degrade to a full scan of chunks per file --
        # O(files x total_chunks) inside the open transaction, which would
        # reintroduce exactly the lock-window problem embedding-outside-the-txn
        # was designed to remove. The UNIQUE also makes delete-and-reinsert safe
        # (no duplicate (file_id, chunk_index) can survive a partial failure).
        "CONSTRAINT uq_chunks_file_id_chunk_index UNIQUE (file_id, chunk_index))"
    )
    # lakebase_ann: vector_cosine_ops is NON-default and MUST be explicit. Do NOT pass
    # WITH (m=..., ef_construction=...) -- lakebase_ann rejects those ("unrecognized
    # parameter"); they are not hnsw params.
    op.execute(
        "CREATE INDEX ix_chunks_embedding_ann ON chunks "
        "USING lakebase_ann (embedding vector_cosine_ops)"
    )
    # lakebase_bm25 over the generated ts column (tsvector_bm25_ops is the default opclass).
    op.execute("CREATE INDEX ix_chunks_ts_bm25 ON chunks USING lakebase_bm25 (ts)")


def downgrade() -> None:
    # Drop the indexes then the table. Intentionally NOT dropping the lakebase_*
    # extensions: they are database-wide objects whose enabling preload is itself
    # irreversible and project-level, mirroring 0001's do-not-drop-the-extension
    # rationale for pg_trgm.
    op.execute("DROP INDEX IF EXISTS ix_chunks_ts_bm25")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_ann")
    op.execute("DROP TABLE IF EXISTS chunks")
