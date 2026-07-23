"""Static source assertions on the linear migrations (no database required).

These guard the hand-edited invariants that an accidental re-autogenerate could
silently undo: the fixed revision ids and ``down_revision`` chain, the ``pg_trgm``
extension being created before any GIN index in ``0001``, the deliberate absence
of any semantic-schema vector / tsvector / chunks surface in ``0001``, and ``0002``'s
frozen backfill literal plus its cadence-filtered ``UPDATE``.

The ``sources`` fixture globs every digit-prefixed migration (``[0-9]*_*.py``)
rather than a single hard-coded prefix, so a newly added revision is actually
read by these assertions instead of silently escaping them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "app" / "alembic" / "versions"
_EXPECTED_REVISIONS = {"0001", "0002", "0003", "0004", "0005"}


@pytest.fixture
def sources() -> dict[str, str]:
    """Map revision-number prefix -> file text for every linear migration."""
    matches = sorted(_VERSIONS_DIR.glob("[0-9]*_*.py"))
    by_prefix = {path.name.split("_", 1)[0]: path.read_text() for path in matches}
    assert set(by_prefix) == _EXPECTED_REVISIONS, (
        f"expected migrations {sorted(_EXPECTED_REVISIONS)}, found {sorted(by_prefix)}; "
        "a new migration must be added here so its invariants are asserted"
    )
    return by_prefix


@pytest.fixture
def source(sources: dict[str, str]) -> str:
    return sources["0001"]


@pytest.fixture
def source_0002(sources: dict[str, str]) -> str:
    return sources["0002"]


@pytest.fixture
def source_0003(sources: dict[str, str]) -> str:
    return sources["0003"]


@pytest.fixture
def source_0005(sources: dict[str, str]) -> str:
    return sources["0005"]


@pytest.mark.unit
def test_revision_identifiers(source: str) -> None:
    assert 'revision: str = "0001"' in source
    assert "down_revision: str | None = None" in source


@pytest.mark.unit
def test_pg_trgm_created_before_first_index(source: str) -> None:
    ext_pos = source.find("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    idx_pos = source.find("op.create_index(")
    assert ext_pos != -1, "missing CREATE EXTENSION IF NOT EXISTS pg_trgm"
    assert idx_pos != -1, "missing op.create_index("
    assert ext_pos < idx_pos, "pg_trgm must be created before the first op.create_index("


@pytest.mark.unit
def test_no_vector_or_phase4_surface(source: str) -> None:
    for forbidden in ("vector", "tsvector", "chunks", "CREATE EXTENSION vector", "DROP EXTENSION"):
        assert forbidden not in source, f"0001 migration must not reference {forbidden!r}"


@pytest.mark.unit
def test_0002_revision_identifiers(source_0002: str) -> None:
    assert 'revision: str = "0002"' in source_0002
    assert 'down_revision: str | None = "0001"' in source_0002


@pytest.mark.unit
def test_0002_backfill_is_cadence_filtered(source_0002: str) -> None:
    assert "index_semantics_version = " in source_0002
    assert "WHERE last_indexed_at > now() - interval '48 hours'" in source_0002, (
        "the backfill must be window-filtered; an unfiltered UPDATE would stamp "
        "stale rows as current"
    )


@pytest.mark.unit
def test_0002_does_not_import_app_constant(source_0002: str) -> None:
    """The backfill literal is frozen; importing the mutable app constant is forbidden."""
    code_lines = [
        line
        for line in source_0002.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in code_lines:
        if line.startswith(("import ", "from ")):
            assert not line.startswith(("import app", "from app")), (
                f"0002 must not import from the app package: {line!r}"
            )
    assert "_BACKFILL_VERSION = 1" in source_0002


@pytest.mark.unit
def test_0002_downgrade_returns_to_0001_shape(source_0002: str) -> None:
    assert 'op.drop_column("repos", "index_semantics_version")' in source_0002


@pytest.mark.unit
def test_0003_revision_identifiers(source_0003: str) -> None:
    assert 'revision: str = "0003"' in source_0003
    assert 'down_revision: str | None = "0002"' in source_0003


@pytest.mark.unit
def test_0003_pgcrypto_created_before_digest(source_0003: str) -> None:
    ext_pos = source_0003.find("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    digest_pos = source_0003.find("digest(coalesce(content")
    assert ext_pos != -1, "missing CREATE EXTENSION IF NOT EXISTS pgcrypto"
    assert digest_pos != -1, "missing digest(coalesce(content...)) backfill"
    assert ext_pos < digest_pos, "pgcrypto must be created before digest() is used"


@pytest.mark.unit
def test_0003_downgrade_guard_runs_before_any_drop(source_0003: str) -> None:
    """The multi-branch-data guard must raise before any destructive DDL runs."""
    downgrade_pos = source_0003.find("def downgrade()")
    guard_pos = source_0003.find("raise RuntimeError(", downgrade_pos)
    first_drop_pos = source_0003.find("op.drop_", downgrade_pos)
    assert downgrade_pos != -1
    assert guard_pos != -1, "missing the dup-guard raise in downgrade()"
    assert first_drop_pos != -1, "missing any op.drop_ call in downgrade()"
    assert guard_pos < first_drop_pos, "the guard must run before any destructive DDL"


@pytest.mark.unit
def test_0003_does_not_import_app_constant(source_0003: str) -> None:
    """A migration is a historical fact; it must not depend on a mutable app constant."""
    code_lines = [
        line
        for line in source_0003.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in code_lines:
        if line.startswith(("import ", "from ")):
            assert not line.startswith(("import app", "from app")), (
                f"0003 must not import from the app package: {line!r}"
            )


@pytest.mark.unit
def test_0005_revision_identifiers(source_0005: str) -> None:
    assert 'revision: str = "0005"' in source_0005
    assert 'down_revision: str | None = "0004"' in source_0005


@pytest.mark.unit
def test_0005_does_not_import_app_constant(source_0005: str) -> None:
    """A migration is a historical fact; it must not depend on a mutable app constant."""
    code_lines = [
        line
        for line in source_0005.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in code_lines:
        if line.startswith(("import ", "from ")):
            assert not line.startswith(("import app", "from app")), (
                f"0005 must not import from the app package: {line!r}"
            )


@pytest.mark.unit
def test_0005_downgrade_drops_indexes_then_table(source_0005: str) -> None:
    downgrade_pos = source_0005.find("def downgrade()")
    assert downgrade_pos != -1
    downgrade_body = source_0005[downgrade_pos:]
    for index_name in (
        "ix_reference_edges_target_name",
        "ix_reference_edges_target_trgm",
        "ix_reference_edges_file_id",
        "ix_reference_edges_repo_kind",
    ):
        assert f'op.drop_index("{index_name}"' in downgrade_body, (
            f"0005 downgrade must drop {index_name!r}"
        )
    table_pos = downgrade_body.find('op.drop_table("reference_edges")')
    assert table_pos != -1, "0005 downgrade must drop the reference_edges table"
    last_index_pos = max(
        downgrade_body.find(f'op.drop_index("{index_name}"')
        for index_name in (
            "ix_reference_edges_target_name",
            "ix_reference_edges_target_trgm",
            "ix_reference_edges_file_id",
            "ix_reference_edges_repo_kind",
        )
    )
    assert last_index_pos < table_pos, "all indexes must drop before the table"


@pytest.mark.unit
def test_0005_no_symbol_fk(source_0005: str) -> None:
    """Epic #82 rule: reference_edges must never gain a symbols FK at the source level."""
    for forbidden in ('ForeignKey("symbols', "REFERENCES symbols", '"symbols.id"'):
        assert forbidden not in source_0005, f"0005 migration must not reference {forbidden!r}"


@pytest.mark.unit
def test_0005_does_not_create_extension(source_0005: str) -> None:
    """pg_trgm already exists since 0001 (database-wide); 0005 must not re-create it."""
    upgrade_pos = source_0005.find("def upgrade()")
    downgrade_pos = source_0005.find("def downgrade()")
    assert upgrade_pos != -1 and downgrade_pos != -1
    body = source_0005[upgrade_pos:downgrade_pos]
    assert "CREATE EXTENSION" not in body
