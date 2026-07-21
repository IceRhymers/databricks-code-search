"""Alembic migration environment for the code search core schema.

Connection resolution (online mode), in priority order:

1. An injected connection at ``config.attributes["connection"]`` wins. This is
   how ``scripts/migrate.py`` runs migrations against Lakebase: it opens the
   engine (which handles OAuth) and hands the live connection to Alembic.
2. Otherwise, if ``LAKEBASE_ENDPOINT`` or ``PGHOST`` is set, fall back to
   ``create_db_engine()`` (its own precedence applies: a configured Lakebase
   endpoint wins). This is the ``make migration`` autogenerate path, run against
   a disposable Lakebase branch.
3. Otherwise raise: there is no safe default, and we never want autogenerate or
   an implicit connection reaching for Lakebase credentials by accident.

The Databricks SDK is never imported here; all connection concerns live in
``app.db.client``.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import Connection

from app.db.models import Base

config = context.config

target_metadata = Base.metadata


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Autogenerate filter: the semantic ``chunks`` surface is invisible to Alembic.

    ``chunks`` deliberately lives outside ``Base.metadata`` (see ``app/db/semantic.py``)
    but exists in any database migrations run against -- including the disposable
    Lakebase branch ``make migration`` autogenerates on -- so without this filter
    autogenerate would emit ``drop_table('chunks')`` (and drops for its indexes).
    """
    if type_ == "table" and name == "chunks":
        return False
    table = getattr(obj, "table", None)
    if table is not None and getattr(table, "name", None) == "chunks":
        return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' (--sql) mode, without a DB connection."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("offline (--sql) mode requires DATABASE_URL to be set")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    version_table_schema = config.attributes.get("version_table_schema")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=version_table_schema,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a real connection."""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return

    if os.environ.get("LAKEBASE_ENDPOINT") or os.environ.get("PGHOST"):
        # The `make migration` autogenerate path: create_db_engine() resolves the
        # target itself (a configured Lakebase endpoint wins over PGHOST).
        from app.db.client import create_db_engine

        engine = create_db_engine()
        try:
            with engine.connect() as conn:
                _run_migrations(conn)
        finally:
            engine.dispose()
        return

    raise RuntimeError(
        "alembic env.py: no injected connection and neither LAKEBASE_ENDPOINT nor PGHOST "
        "is set. Run migrations via scripts/migrate.py (injected connection); for "
        "autogenerate, point LAKEBASE_ENDPOINT at a disposable Lakebase branch."
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
