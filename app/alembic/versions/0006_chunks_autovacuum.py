"""chunks autovacuum tuning for the BM25 workload

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02 00:00:00.000000

Applies the Lakebase Search GA recommendation for search-dedicated tables
(https://docs.databricks.com/aws/en/oltp/projects/lakebase-text#keep-the-index-accurate):

    ALTER TABLE chunks SET (autovacuum_vacuum_insert_scale_factor = 0);

Why: ``lakebase_bm25`` corpus statistics are computed at index build time and refreshed
by VACUUM, so prompt vacuuming of ``chunks`` keeps BM25 ranking accurate and the index
performant. The default ``autovacuum_vacuum_insert_scale_factor`` (0.2) lets the
insert-triggered autovacuum threshold GROW WITH TABLE SIZE -- wrong for ``chunks``,
whose write path is a bulk delete-and-reinsert of a large fraction of the table on
every index run. Setting the scale factor to 0 makes the insert trigger a fixed count
(``autovacuum_vacuum_insert_threshold``) instead, so autovacuum fires on the workload's
insert volume, not its table size.

Schema-only, no grant coupling: an ``ALTER TABLE ... SET (...)`` storage-parameter
change creates no new objects, so the existing schema-wide grants already cover it.

Idempotent: ``ALTER TABLE ... SET (...)`` is a no-op when the value is already set, so
this is safe to re-run. Guarded by ``to_regclass('chunks')`` so a project that has not
yet applied ``0004`` (or downgraded past it) skips cleanly rather than erroring.

See ``docs/runbooks/lakebase-search-ops.md`` §3 for the operational context.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.execute(text("SELECT to_regclass('chunks')")).scalar() is None:
        return
    op.execute("ALTER TABLE chunks SET (autovacuum_vacuum_insert_scale_factor = 0)")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(text("SELECT to_regclass('chunks')")).scalar() is None:
        return
    # Reset removes the per-table override, restoring the cluster default (0.2).
    op.execute("ALTER TABLE chunks RESET (autovacuum_vacuum_insert_scale_factor)")
