"""C1 regression: the semantic head must live in a SEPARATE version table.

If the gated semantic head is ever recorded in the core ``alembic_version`` table, a
later core ``command.upgrade(config, "head")`` (the exact call ``scripts/migrate.py``
makes) resolves that unknown head against the core-only ScriptDirectory and raises
``CommandError`` -- permanently breaking core migrations on every future deploy to an
enabled project. This test reproduces the post-enablement scenario against real local
Postgres and asserts the isolation holds: with the semantic head in
``alembic_version_semantic``, a default core ``upgrade head`` still succeeds.

It drives the SAME separate-Config + ``version_table="alembic_version_semantic"`` path
``scripts/migrate.py --semantic`` uses, but through a TEST-ONLY stand-in revision (see
``fixtures/versions_semantic_standin/``) so no beta ``lakebase_*`` extension is needed.
Would fail if someone regressed the version-table isolation (e.g. dropped the
``version_table`` thread in ``env.py``) back to the shared table.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from app.db.client import create_db_engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_VERSIONS = str(_REPO_ROOT / "app" / "alembic" / "versions")
_STANDIN_VERSIONS = str(Path(__file__).resolve().parent / "fixtures" / "versions_semantic_standin")


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _core_head() -> str:
    """The current core head, derived -- never hardcoded.

    This assertion used to compare against the literal ``"0001"``, which was merely
    the core head on the day the test was written. Adding ``0002`` broke it even
    though the invariant it guards (the SEMANTIC head must never land in the core
    table) still held perfectly. Deriving the head keeps the test about isolation
    rather than about whichever revision happens to be newest.
    """
    from alembic.script import ScriptDirectory

    config = Config("alembic.ini")
    config.set_main_option("version_locations", _CORE_VERSIONS)
    return ScriptDirectory.from_config(config).get_current_head()


def _core_config(conn: Connection, schema: str) -> Config:
    """A DEFAULT core Config: version_table unset -> Alembic's 'alembic_version'."""
    config = Config("alembic.ini")
    config.attributes["connection"] = conn
    config.attributes["version_table_schema"] = schema
    return config


@pytest.fixture
def core_migrated() -> Iterator[tuple[Connection, str]]:
    """Throwaway schema with the core schema applied (alembic_version = {0001})."""
    schema = _unique("test_c1")
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.commit()

        command.upgrade(_core_config(conn, schema), "head")
        conn.commit()

        yield conn, schema
    finally:
        conn.rollback()
        # Restore the clean "no vector extension" invariant other tests assert; the
        # stand-in installs the DB-wide pgvector extension.
        conn.execute(text("DROP EXTENSION IF EXISTS vector CASCADE"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_core_upgrade_head_survives_recorded_semantic_head(
    core_migrated: tuple[Connection, str],
) -> None:
    conn, schema = core_migrated

    # (b) Record a semantic head into the SEPARATE alembic_version_semantic table via the
    # stand-in revision, driving the same separate-Config path scripts/migrate.py uses.
    sem = Config("alembic.ini")
    sem.set_main_option("version_locations", os.pathsep.join([_CORE_VERSIONS, _STANDIN_VERSIONS]))
    sem.attributes["connection"] = conn
    sem.attributes["version_table_schema"] = schema
    sem.attributes["version_table"] = "alembic_version_semantic"
    command.upgrade(sem, "semantic@head")
    conn.commit()

    # The isolation invariant: the semantic head landed in its OWN table, never the core one.
    core_heads = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    sem_heads = (
        conn.execute(text("SELECT version_num FROM alembic_version_semantic")).scalars().all()
    )
    assert core_heads == [_core_head()], (
        f"semantic head leaked into core alembic_version: {core_heads}"
    )
    assert sem_heads == ["0002semtest"]

    # (c) The exact core call scripts/migrate.py makes must still succeed -- under the old
    # shared-table bug this raised CommandError on the unknown '0002semtest'.
    command.upgrade(_core_config(conn, schema), "head")
    conn.commit()

    # And no beta lakebase_* extension is present (the stand-in never creates one).
    lakebase = conn.execute(
        text("SELECT count(*) FROM pg_extension WHERE extname LIKE 'lakebase%'")
    ).scalar()
    assert lakebase == 0
