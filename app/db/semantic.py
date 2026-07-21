"""Standalone Core schema for the semantic-search surface (issue #14).

``chunks`` lives in its OWN ``MetaData()`` instance -- it is deliberately NOT added to
``app.db.models.Base.metadata``. The DDL is hand-written: ``chunks.embedding`` is a
``vector`` column and ``chunks.ts`` a ``GENERATED`` ``tsvector`` column backed by the
``lakebase_vector`` / ``lakebase_text`` extensions, none of which SQLAlchemy Core can
declare portably. If ``chunks`` lived in ``Base.metadata``, a routine ``make migration``
would try to diff it against what the hand-written ``0004`` revision actually created,
producing spurious drift -- the ``include_object`` filter in ``app/alembic/env.py`` is
the second half of that protection (any database autogenerate runs against now contains
``chunks``, and without the filter autogenerate would emit ``drop_table('chunks')``).

This module declares the Core ``Table`` only -- it is a typed description of the
production shape (and the read/write path's expectations), not a migration. The ``0004``
core revision (``app/alembic/versions/0004_semantic_chunks.py``) is the sole owner of
the real DDL.
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
    # 1-based inclusive line range of the chunk within its file (issue #44). Nullable:
    # rows written before the line-aware writer stay NULL until naturally re-indexed,
    # and readers must treat NULL as "no authoritative range".
    Column("start_line", Integer, nullable=True),
    Column("end_line", Integer, nullable=True),
    Column("embedding", Vector(SEMANTIC_EMBEDDING_DIM)),
    # Nullable here: this Table only declares the read/write-path column type. The 0004
    # migration makes this a GENERATED column -- `ts tsvector GENERATED ALWAYS AS
    # (to_tsvector('english', content)) STORED` -- backed by the lakebase_text
    # extension; SQLAlchemy Core has no portable way to declare GENERATED columns, so
    # the DDL for that lives entirely in the hand-written revision, not here.
    Column("ts", TSVECTOR, nullable=True),
    # Mirrors the 0004 revision's constraint. Load-bearing for WRITE performance,
    # not just integrity: Postgres does not auto-index a foreign key, and both
    # chunk_store's per-file DELETE and the mark-and-sweep ON DELETE CASCADE look
    # rows up by file_id. Without this file_id-leading index they full-scan chunks
    # once per file, inside the open transaction.
    UniqueConstraint("file_id", "chunk_index", name="uq_chunks_file_id_chunk_index"),
)
