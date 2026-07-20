"""Static source assertions on the linear migrations (no database required).

These guard the hand-edited invariants that an accidental re-autogenerate could
silently undo: the fixed revision ids and ``down_revision`` chain, the ``pg_trgm``
extension being created before any GIN index in ``0001``, the deliberate absence
of any Phase-4 vector / tsvector / chunks surface in ``0001``, and ``0002``'s
frozen backfill literal plus its cadence-filtered ``UPDATE``.

The ``sources`` fixture globs every digit-prefixed migration (``[0-9]*_*.py``)
rather than a single hard-coded prefix, so a newly added revision is actually
read by these assertions instead of silently escaping them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "app" / "alembic" / "versions"
_EXPECTED_REVISIONS = {"0001", "0002"}


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
