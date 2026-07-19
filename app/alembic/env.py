"""Alembic migration environment for the code search core schema.

Connection resolution (online mode), in priority order:

1. An injected connection at ``config.attributes["connection"]`` wins. This is
   how ``scripts/migrate.py`` runs migrations against Lakebase: it opens the
   engine (which handles OAuth) and hands the live connection to Alembic.
2. Otherwise, if ``PGHOST`` is set, fall back to ``create_db_engine()`` for a
   plain local Postgres run (CI, ``make migrate-local``, integration tests).
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


def run_migrations_offline() -> None:
    """Run migrations in 'offline' (--sql) mode, without a DB connection."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("offline (--sql) mode requires DATABASE_URL to be set")
    # See _run_migrations: version_table has no None default, so coerce an unset
    # attribute (the core path) to Alembic's default "alembic_version".
    version_table = config.attributes.get("version_table") or "alembic_version"
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=version_table,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    version_table_schema = config.attributes.get("version_table_schema")
    # Optional separate version table. Set ONLY by scripts/migrate.py --semantic to
    # "alembic_version_semantic" so the gated semantic head is NEVER recorded in the
    # core "alembic_version"; if it were, a later core `upgrade head` would try to
    # resolve the semantic head against the core-only ScriptDirectory and raise
    # CommandError, permanently breaking core migrations. Unlike version_table_schema
    # (whose default IS None), version_table has NO None default -- passing None breaks
    # Table() construction on the core path -- so it must coerce to Alembic's default
    # "alembic_version" when the attribute is unset (the core path).
    version_table = config.attributes.get("version_table") or "alembic_version"
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=version_table_schema,
        version_table=version_table,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a real connection."""
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return

    if os.environ.get("PGHOST"):
        # Local Postgres fallback; create_db_engine() never imports the SDK here.
        from app.db.client import create_db_engine

        engine = create_db_engine()
        try:
            with engine.connect() as conn:
                _run_migrations(conn)
        finally:
            engine.dispose()
        return

    raise RuntimeError(
        "alembic env.py: no injected connection and PGHOST unset. Run Lakebase "
        "migrations via scripts/migrate.py (injected connection); for local, set PGHOST."
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
