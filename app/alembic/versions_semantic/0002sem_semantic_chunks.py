"""semantic chunks (gated, separate history)

Revision ID: 0002sem
Revises:
Create Date: 2026-07-19 13:40:00.000000

GATED, IRREVERSIBLE semantic-search surface for issue #14. This revision is
DELIBERATELY isolated from the core migration graph and MUST NOT reach the core
``make migrate`` path:

* ``down_revision = None`` + ``branch_labels = ("semantic",)`` and NO ``depends_on``
  -- it is a standalone branch head, never a child of ``0001``. A separate version
  table (``alembic_version_semantic``, wired in ``scripts/migrate.py --semantic``
  and ``env.py``) means Alembic never records this head in the core
  ``alembic_version``; if it did, a later core ``upgrade head`` would try to resolve
  ``0002sem`` against the core-only ScriptDirectory and raise ``CommandError``,
  permanently breaking core migrations on every enabled project. The 0001->chunks
  ordering is therefore enforced OPERATIONALLY (the ``--semantic`` entrypoint
  pre-checks that ``files`` exists) and structurally (the FK below), not by a graph
  edge -- a ``depends_on`` edge would make Alembic try to RE-APPLY 0001 into the
  separate history and fail with "relation repos already exists".

* **The real per-project enablement is NOT this migration.** ``lakebase_vector`` and
  ``lakebase_text`` load only when the Databricks-MANAGED ``shared_preload_libraries``
  already includes them. That preload change is irreversible, project-level, and
  performed out-of-band (UI/support) -- it is a PREREQUISITE this migration cannot
  perform. When the preload is absent, ``CREATE EXTENSION`` fails loudly with
  "must be loaded via shared_preload_libraries"; that error IS the intended signal to
  go complete the managed-preload prerequisite first. See
  ``docs/runbooks/semantic-enablement.md``.

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

from app.config import SEMANTIC_EMBEDDING_DIM

# revision identifiers, used by Alembic.
revision: str = "0002sem"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = ("semantic",)
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
