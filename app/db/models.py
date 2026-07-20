"""SQLAlchemy 2.0 models for the durable code search core.

Covers ``repos`` / ``files`` / ``symbols`` only. No ``chunks`` / ``VECTOR`` /
``tsvector`` (Phase 4). ``Base.metadata`` is the authoritative desired-state:
it also declares the three pg_trgm GIN indexes so issue #5's Alembic autogenerate
emits them and there is no future drift. Issue #5 owns only the single
``CREATE EXTENSION IF NOT EXISTS pg_trgm`` (sequenced before index creation).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

INDEX_SEMANTICS_VERSION = 1
"""Version of the indexing semantics the current code produces.

Bump this whenever the *meaning* of what gets written changes: any change to
``indexer/symbols.py``, to ``indexer/parse.py``'s chunking, or to
``indexer/languages.py``'s extraction contract. A bump forces every repo to
re-index once, because a repo's stored ``repos.index_semantics_version`` no
longer matches. The CI tripwire enforces the bump obligation.

Migrations must never import this constant -- see
``app/alembic/versions/0002_index_semantics_version.py``.
"""


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    default_branch: Mapped[str | None] = mapped_column(Text)
    last_indexed_commit: Mapped[str | None] = mapped_column(Text)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL means "provenance unknown -> always reindex".
    index_semantics_version: Mapped[int | None] = mapped_column(Integer)

    files: Mapped[list[File]] = relationship(back_populates="repo", cascade="all, delete-orphan")


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("repo_id", "path", name="uq_files_repo_id_path"),
        Index(
            "ix_files_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ),
        Index(
            "ix_files_path_trgm",
            "path",
            postgresql_using="gin",
            postgresql_ops={"path": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    lang: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text)
    commit: Mapped[str | None] = mapped_column(Text)

    repo: Mapped[Repo] = relationship(back_populates="files")
    symbols: Mapped[list[Symbol]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )


class Symbol(Base):
    __tablename__ = "symbols"
    __table_args__ = (
        Index(
            "ix_symbols_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(Text)
    start_line: Mapped[int | None] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)

    file: Mapped[File] = relationship(back_populates="symbols")
