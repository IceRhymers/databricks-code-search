"""Standalone Core schema for the semantic-search beta extension (issue #14).

``chunks`` lives in its OWN ``MetaData()`` instance -- it is deliberately NOT added to
``app.db.models.Base.metadata``. Two reasons, both load-bearing:

* **Beta-extension isolation.** ``chunks.embedding`` is a pgvector ``Vector`` and (in
  production) ``chunks.ts`` is a ``GENERATED`` column backed by the beta
  ``lakebase_vector`` / ``lakebase_text`` extensions. The core schema (``repos`` /
  ``files`` / ``symbols``) must never reference a beta extension, so core deploys never
  depend on -- or accidentally enable -- semantic search.
* **Autogenerate-drift avoidance.** ``Base.metadata`` is the desired-state Alembic
  autogenerates the core migration graph against (see ``app/db/models.py``). If
  ``chunks`` lived there, a routine ``make migration`` would try to diff it against
  whatever the gated, hand-written semantic revision actually created, producing
  spurious drift. Keeping ``chunks`` in its own ``MetaData`` means core autogenerate
  never sees it; the semantic revision under ``app/alembic/versions_semantic/`` is
  hand-written and is the sole owner of its DDL.

This module declares the Core ``Table`` only -- it is a typed description of the
production shape (and the read/write path's expectations), not a migration. The gated
revision creates the real table with the beta ``lakebase_vector`` / ``lakebase_text``
extensions; a test-only stand-in (pgvector ``vector`` + a plain ``tsvector`` column) is
built from this same ``Table`` for CI, which lacks those extensions.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR

from app.config import SEMANTIC_EMBEDDING_DIM

# Deliberately separate from app.db.models.Base.metadata -- see module docstring.
semantic_metadata = MetaData()

chunks = Table(
    "chunks",
    semantic_metadata,
    # bigserial PK: a repo's chunk count can exceed int32 across many large repos.
    Column("id", BigInteger, primary_key=True),
    Column("file_id", Integer, ForeignKey("files.id", ondelete="CASCADE"), nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("embedding", Vector(SEMANTIC_EMBEDDING_DIM)),
    # Nullable here: this Table only declares the read/write-path column type. In
    # production the gated migration (app/alembic/versions_semantic/) makes this a
    # GENERATED column -- `ts tsvector GENERATED ALWAYS AS
    # (to_tsvector('english', content)) STORED` -- backed by the beta lakebase_text
    # extension; SQLAlchemy Core has no portable way to declare GENERATED columns, so
    # the DDL for that lives entirely in the hand-written revision, not here.
    Column("ts", TSVECTOR, nullable=True),
    # Mirrors the gated revision's constraint. Load-bearing for WRITE performance,
    # not just integrity: Postgres does not auto-index a foreign key, and both
    # chunk_store's per-file DELETE and the mark-and-sweep ON DELETE CASCADE look
    # rows up by file_id. Without this file_id-leading index they full-scan chunks
    # once per file, inside the open transaction.
    UniqueConstraint("file_id", "chunk_index", name="uq_chunks_file_id_chunk_index"),
)
