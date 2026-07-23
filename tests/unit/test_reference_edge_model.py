"""Model-level tripwires for ``reference_edges`` (no database required).

Introspects ``Base.metadata`` directly, mirroring ``test_migration_source.py``'s
source-level checks but at the ORM-metadata layer: a future edit to
``app/db/models.py`` that accidentally introduces a ``symbols`` FK, drops the
CHECK constraint, or loosens a NOT NULL column fails here, not in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import BigInteger, CheckConstraint, ForeignKeyConstraint

from app.db.models import Base, File

_TABLE = Base.metadata.tables["reference_edges"]


@pytest.mark.unit
def test_reference_edges_fk_targets_only_repos_and_files() -> None:
    fk_targets = {fk.target_fullname for fk in _TABLE.foreign_keys}
    assert fk_targets == {"repos.id", "files.id"}, (
        "reference_edges must never gain a symbols FK (epic #82 rule): "
        f"found FK targets {fk_targets}"
    )


@pytest.mark.unit
def test_reference_edges_fks_cascade_on_delete() -> None:
    for constraint in _TABLE.constraints:
        if isinstance(constraint, ForeignKeyConstraint):
            for element in constraint.elements:
                assert element.ondelete == "CASCADE", (
                    f"FK to {element.target_fullname!r} must be ON DELETE CASCADE"
                )


@pytest.mark.unit
def test_reference_edges_id_is_bigint() -> None:
    assert isinstance(_TABLE.c.id.type, BigInteger)


@pytest.mark.unit
def test_reference_edges_not_null_columns() -> None:
    required = {"repo_id", "file_id", "edge_kind", "target_name", "line"}
    nullable = {"enclosing_name", "enclosing_kind", "enclosing_start_line", "enclosing_end_line"}
    for name in required:
        assert not _TABLE.c[name].nullable, f"{name} must be NOT NULL"
    for name in nullable:
        assert _TABLE.c[name].nullable, f"{name} must be nullable (module/top-level scope)"


@pytest.mark.unit
def test_reference_edges_edge_kind_check_constraint() -> None:
    checks = [c for c in _TABLE.constraints if isinstance(c, CheckConstraint)]
    assert len(checks) == 1
    check = checks[0]
    assert check.name == "ck_reference_edges_edge_kind"
    assert "call" in str(check.sqltext) and "import" in str(check.sqltext)


@pytest.mark.unit
def test_reference_edges_indexes() -> None:
    indexes = {ix.name: ix for ix in _TABLE.indexes}
    expected_names = {
        "ix_reference_edges_target_name",
        "ix_reference_edges_target_trgm",
        "ix_reference_edges_file_id",
        "ix_reference_edges_repo_kind",
    }
    assert set(indexes) == expected_names

    assert [c.name for c in indexes["ix_reference_edges_target_name"].columns] == ["target_name"]
    assert [c.name for c in indexes["ix_reference_edges_file_id"].columns] == ["file_id"]
    assert [c.name for c in indexes["ix_reference_edges_repo_kind"].columns] == [
        "repo_id",
        "edge_kind",
    ]

    trgm = indexes["ix_reference_edges_target_trgm"]
    assert [c.name for c in trgm.columns] == ["target_name"]
    assert trgm.dialect_options["postgresql"]["using"] == "gin"
    assert trgm.dialect_options["postgresql"]["ops"] == {"target_name": "gin_trgm_ops"}


@pytest.mark.unit
def test_file_reference_edges_relationship_cascades() -> None:
    rel = File.reference_edges
    assert rel.property.cascade.delete_orphan
