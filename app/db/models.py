"""SQLAlchemy 2.0 models for the durable code search core.

Covers ``repos`` / ``files`` / ``symbols`` / ``repo_branches``. No ``chunks`` /
``VECTOR`` / ``tsvector`` (Phase 4, separate version table). ``Base.metadata`` is
the authoritative desired-state: it also declares the pg_trgm and
``files.branches`` GIN indexes so Alembic autogenerate emits them and there is
no future drift. Issue #5 owns the single ``CREATE EXTENSION IF NOT EXISTS
pg_trgm``; ``pgcrypto`` is created by the ``0003`` migration only (sequenced
before its digest-based backfill).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

INDEX_SEMANTICS_VERSION = 2
"""Version of the indexing semantics the current code produces.

2: semantic search default-on -- every already-indexed branch must re-index once so
``chunks`` backfills (the skip seam compares ``(head_sha, INDEX_SEMANTICS_VERSION)``,
and without a bump a repo already at HEAD would skip forever and never get chunks).

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
    # DEPRECATED (multi-branch, 0003): the authoritative per-branch stamp lives on
    # ``repo_branches`` now, CAS-guarded there. These three columns are written by
    # the default-branch run only, WITHOUT CAS, for one release so ``list_repos``'
    # legacy field and any 0002-era reader degrade gracefully. A later cleanup
    # migration drops them -- do not add new readers/writers of these three.
    last_indexed_commit: Mapped[str | None] = mapped_column(Text)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # NULL means "provenance unknown -> always reindex".
    index_semantics_version: Mapped[int | None] = mapped_column(Integer)

    files: Mapped[list[File]] = relationship(back_populates="repo", cascade="all, delete-orphan")
    repo_branches: Mapped[list[RepoBranch]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )


class File(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("repo_id", "path", "content_sha", name="uq_files_repo_path_sha"),
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
        Index("ix_files_branches_gin", "branches", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(Text)
    lang: Mapped[str | None] = mapped_column(Text)
    size: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text)
    # AMBIGUOUS under multi-branch dedup (0003): a content version shared across
    # branches with different head SHAs has no single meaningful commit -- this
    # records only the last branch head that upserted the row. Write-only; never
    # read as a sweep key or a source of truth.
    commit: Mapped[str | None] = mapped_column(Text)
    content_sha: Mapped[str] = mapped_column(Text)
    branches: Mapped[list[str]] = mapped_column(ARRAY(Text))

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


class RepoBranch(Base):
    """Per-(repo, branch) index registry: the authoritative CAS stamp (0003+).

    Replaces the single ``repos``-level stamp as the source of truth for
    skip-if-unchanged and ``StaleIndexError`` guarding -- one row per branch a
    repo's config resolves to, indexed independently (plan Option A1: branches
    are sequential within a repo, so no same-repo concurrent writer ever exists).
    """

    __tablename__ = "repo_branches"
    __table_args__ = (UniqueConstraint("repo_id", "branch", name="uq_repo_branches"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    branch: Mapped[str] = mapped_column(Text)
    last_indexed_commit: Mapped[str | None] = mapped_column(Text)
    index_semantics_version: Mapped[int | None] = mapped_column(Integer)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    repo: Mapped[Repo] = relationship(back_populates="repo_branches")
