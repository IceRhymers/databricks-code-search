"""reference edges (raw, unresolved call/import edges)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-23 00:00:00.000000

Adds ``reference_edges``: one row per raw (unresolved) call/import site found by
the extractor (epic #82). Deliberately NO FK to ``symbols`` -- symbol ids churn
on every per-file delete-and-reinsert, and an FK would couple the two rewrite
orders inside the indexing transaction for no query benefit. Resolution from
``target_name`` to a concrete symbol happens at query time by name-join (a
later child of #82), not here. The enclosing symbol is denormalized onto the
row instead (``enclosing_*``, all nullable -- NULL means module/top-level
scope). No ``branches`` column: branch membership rides ``files.branches`` at
query time, exactly as ``symbols`` does.

``pg_trgm`` already exists (created by 0001, database-wide) so no
``CREATE EXTENSION`` is needed here; extension-before-index ordering is
satisfied by the chain itself.

**Grant coupling (not schema-only for an already-deployed target):** the
schema-wide grant builders in ``app/db/grants.py`` (``GRANT ... ON ALL
TABLES IN SCHEMA`` + ``ALTER DEFAULT PRIVILEGES``) cover this new table
automatically ONLY when the same identity that ran the original grants also
runs this migration (Postgres ADP binds to the executing role). A different
identity running a schema-only ``make migrate`` needs an explicit re-grant.
See ``docs/runbooks/reference-edges.md`` for the verification query and the
re-grant command.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reference_edges",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("edge_kind", sa.Text(), nullable=False),
        sa.Column("target_name", sa.Text(), nullable=False),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("enclosing_name", sa.Text(), nullable=True),
        sa.Column("enclosing_kind", sa.Text(), nullable=True),
        sa.Column("enclosing_start_line", sa.Integer(), nullable=True),
        sa.Column("enclosing_end_line", sa.Integer(), nullable=True),
        sa.CheckConstraint("edge_kind IN ('call', 'import')", name="ck_reference_edges_edge_kind"),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reference_edges_target_name",
        "reference_edges",
        ["target_name"],
        unique=False,
    )
    op.create_index(
        "ix_reference_edges_target_trgm",
        "reference_edges",
        ["target_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"target_name": "gin_trgm_ops"},
    )
    # Load-bearing for write performance, not just integrity: Postgres does NOT
    # auto-index a foreign key, and file_id is the hot lookup for both the
    # per-file delete-and-reinsert writer and the ON DELETE CASCADE fired by
    # store.py's mark-and-sweep -- same rationale as ix_symbols' analog on
    # symbols.file_id (there via the implicit FK) and uq_chunks_file_id_chunk_index.
    op.create_index(
        "ix_reference_edges_file_id",
        "reference_edges",
        ["file_id"],
        unique=False,
    )
    op.create_index(
        "ix_reference_edges_repo_kind",
        "reference_edges",
        ["repo_id", "edge_kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reference_edges_repo_kind", table_name="reference_edges")
    op.drop_index("ix_reference_edges_file_id", table_name="reference_edges")
    op.drop_index("ix_reference_edges_target_trgm", table_name="reference_edges")
    op.drop_index("ix_reference_edges_target_name", table_name="reference_edges")
    op.drop_table("reference_edges")
