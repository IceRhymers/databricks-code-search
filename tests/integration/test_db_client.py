"""Integration tests for the engine factory against a Lakebase branch.

Exercises the engine factory end-to-end and asserts real DDL fidelity via
pg_catalog / information_schema after ``create_all()`` (not just metadata
reflection). Requires ``LAKEBASE_ENDPOINT`` pointing at a disposable branch
(the ci-lakebase.yml pattern).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text

from app.db.client import create_db_engine
from app.db.models import Base

SCHEMA = "test_db_client"


@pytest.fixture
def conn() -> Iterator[Connection]:
    """Yield a connection with a clean throwaway schema + the durable-core DDL.

    Idempotent: the schema is dropped and recreated each run. pg_trgm is created
    in the default (public) schema so ``gin_trgm_ops`` is resolvable via the
    search_path while the tables live in the isolated test schema.
    """
    engine = create_db_engine()
    connection = engine.connect()
    try:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        connection.execute(text(f"SET search_path TO {SCHEMA}, public"))
        connection.commit()

        Base.metadata.create_all(bind=connection)
        connection.commit()

        yield connection
    finally:
        connection.rollback()
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.commit()
        connection.close()
        engine.dispose()


@pytest.mark.integration
def test_select_1() -> None:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()


@pytest.mark.integration
def test_tables_created(conn: Connection) -> None:
    rows = (
        conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
            {"s": SCHEMA},
        )
        .scalars()
        .all()
    )
    assert {"repos", "files", "symbols"} <= set(rows)


@pytest.mark.integration
def test_files_repo_foreign_key_enforced(conn: Connection) -> None:
    count = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            "  AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' "
            "  AND tc.table_schema = :s AND tc.table_name = 'files' "
            "  AND kcu.column_name = 'repo_id'"
        ),
        {"s": SCHEMA},
    ).scalar()
    assert count and count >= 1


@pytest.mark.integration
def test_unique_repo_id_path(conn: Connection) -> None:
    cols = (
        conn.execute(
            text(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'UNIQUE' "
                "  AND tc.table_schema = :s AND tc.table_name = 'files'"
            ),
            {"s": SCHEMA},
        )
        .scalars()
        .all()
    )
    assert {"repo_id", "path"} <= set(cols)


@pytest.mark.integration
def test_trgm_gin_indexes_present(conn: Connection) -> None:
    rows = conn.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s"),
        {"s": SCHEMA},
    ).all()
    by_name = {name: definition for name, definition in rows}

    for idx in ("ix_files_content_trgm", "ix_files_path_trgm", "ix_symbols_name_trgm"):
        assert idx in by_name, f"missing index {idx}"
        definition = by_name[idx]
        assert "USING gin" in definition
        assert "gin_trgm_ops" in definition
