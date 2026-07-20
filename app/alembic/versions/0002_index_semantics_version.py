"""index semantics version

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-20 00:00:00.000000

Adds ``repos.index_semantics_version``: the version of the indexing semantics
(symbol extraction, chunking, language extraction contract) under which a repo's
rows were last written. NULL means "provenance unknown -> always reindex".

Unlike ``0001`` this migration is *data-modifying*: it backfills the new column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The backfill value is deliberately FROZEN at the value current as of this
# migration. This module must NEVER import ``INDEX_SEMANTICS_VERSION`` from the
# application: a migration is a historical fact and must not depend on a mutable
# app constant, or replaying history on a fresh database would stamp rows with
# whatever version happens to be current today.
_BACKFILL_VERSION = 1


def upgrade() -> None:
    op.add_column("repos", sa.Column("index_semantics_version", sa.Integer(), nullable=True))
    # Backfill by CADENCE, not provenance: under pre-plan code every run
    # re-indexed every repo unconditionally, so any row the last run touched was
    # written by the code shipping today. The window deliberately EXCLUDES rows
    # the recent run did not rewrite -- a repo whose HEAD has not moved and whose
    # last index predates a parser change is stale in a way the SHA comparison
    # structurally cannot see. Those stay NULL and re-index once.
    op.execute(
        f"UPDATE repos SET index_semantics_version = {_BACKFILL_VERSION} "
        "WHERE last_indexed_at > now() - interval '48 hours'"
    )


def downgrade() -> None:
    # Returns ``repos`` to its exact 0001 shape.
    op.drop_column("repos", "index_semantics_version")
