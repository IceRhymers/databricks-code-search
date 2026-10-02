"""Static source assertions on the ``0006`` chunks-autovacuum revision (no database).

``0006`` is a one-statement storage-parameter change; the guard here is that its hand-
written invariants (revision chain, the chunks guard, schema-only/no-new-objects) are
not silently edited away. Mirrors ``test_migration_source_semantic.py``'s pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_VERSIONS = Path(__file__).resolve().parents[2] / "app" / "alembic" / "versions"


@pytest.fixture
def source() -> str:
    matches = sorted(_VERSIONS.glob("0006*.py"))
    assert len(matches) == 1, f"expected exactly one 0006*.py, found {matches}"
    return matches[0].read_text()


@pytest.mark.unit
def test_revision_is_core_chained(source: str) -> None:
    """0006 is a plain core revision: chained off 0005, no branch labels, no depends_on."""
    assert 'revision: str = "0006"' in source
    assert 'down_revision: str | None = "0005"' in source
    assert "branch_labels: str | Sequence[str] | None = None" in source
    assert "depends_on: str | Sequence[str] | None = None" in source


@pytest.mark.unit
def test_sets_the_ga_recommended_insert_scale_factor(source: str) -> None:
    """The whole point of 0006: the lakebase_text GA recommendation for search tables."""
    assert "ALTER TABLE chunks SET (autovacuum_vacuum_insert_scale_factor = 0)" in source


@pytest.mark.unit
def test_guarded_by_to_regclass_chunks(source: str) -> None:
    """A project that has not applied 0004 (or downgraded past it) must skip cleanly."""
    upgrade = source[source.index("def upgrade()") : source.index("def downgrade()")]
    assert "to_regclass('chunks')" in upgrade


@pytest.mark.unit
def test_creates_no_new_objects(source: str) -> None:
    """Schema-only: no new tables/indexes/extensions, so existing grants still cover it."""
    upgrade = source[source.index("def upgrade()") : source.index("def downgrade()")]
    for forbidden in ("CREATE TABLE", "CREATE INDEX", "CREATE EXTENSION", "ADD COLUMN"):
        assert forbidden not in upgrade, f"0006 must not create objects: {forbidden}"


@pytest.mark.unit
def test_downgrade_resets_the_override(source: str) -> None:
    """Downgrade restores the cluster default by removing the per-table override."""
    downgrade = source[source.index("def downgrade()") :]
    assert "ALTER TABLE chunks RESET (autovacuum_vacuum_insert_scale_factor)" in downgrade
