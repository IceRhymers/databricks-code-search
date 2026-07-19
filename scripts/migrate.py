"""Apply the code search schema migrations, optionally with least-privilege grants.

This is the single entry point for running Alembic against Lakebase: it opens the
engine via :func:`app.db.client.create_db_engine` (which handles OAuth for
Lakebase and plain PG* for local), injects the live connection into Alembic, and
runs ``upgrade head``. The same script works locally when ``PGHOST`` is set.

Grants are opt-in via ``--apply-grants`` and default OFF, so a routine migration
never touches role privileges. When enabled, the app (read-only) role named by
``APP_SP_ROLE`` and the job (write) role named by ``JOB_WRITER_ROLE`` are applied
*independently*: each provided role is validated, checked for existence in
``pg_roles``, and granted least privilege on the target schema. At least one of the
two must be set (dev grants the app only; prod grants both); it is an error to pass
``--apply-grants`` with neither set.

No Databricks SDK import happens at module scope; credential handling stays inside
``app.db.client``. Role tokens/passwords are never logged.
"""

from __future__ import annotations

import argparse
import logging
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from app.db.client import create_db_engine
from app.db.grants import build_app_grants, build_job_grants, quote_ident, validate_role

logger = logging.getLogger("migrate")


def _resolve_schema(connection: Connection) -> str:
    """Resolve the target schema for the migration DDL and grants.

    ``PGSCHEMA`` when set, else ``current_schema()``. This single value drives
    the connection ``search_path``, the Alembic version table, and the grants,
    so the tables the migration creates and the tables the grants target are
    always the same schema.
    """
    schema = os.environ.get("PGSCHEMA")
    if schema:
        return schema
    current = connection.execute(text("SELECT current_schema()")).scalar()
    if not current:
        raise RuntimeError("could not resolve a target schema: current_schema() is NULL")
    return str(current)


def _assert_role_exists(connection: Connection, role: str) -> None:
    exists = connection.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).scalar()
    if not exists:
        raise RuntimeError(f"role {role!r} does not exist in pg_roles; create it before granting")


def _apply_grants(connection: Connection, schema: str) -> None:
    app_env = os.environ.get("APP_SP_ROLE")
    job_env = os.environ.get("JOB_WRITER_ROLE")
    if not app_env and not job_env:
        raise RuntimeError("--apply-grants requires at least one of APP_SP_ROLE / JOB_WRITER_ROLE")
    if app_env:
        app_role = validate_role(app_env)
        _assert_role_exists(connection, app_role)
        for stmt in build_app_grants(schema, app_role):
            connection.execute(text(stmt))
        logger.info("grants: applied app read-only grants to role %s", app_role)
    if job_env:
        job_role = validate_role(job_env)
        _assert_role_exists(connection, job_role)
        for stmt in build_job_grants(schema, job_role):
            connection.execute(text(stmt))
        logger.info("grants: applied job write grants to role %s", job_role)


def run(apply_grants: bool) -> None:
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            schema = _resolve_schema(connection)
            # Pin DDL, version table, and grants to one schema so they can't diverge.
            connection.execute(text(f"SET search_path TO {quote_ident(schema)}, public"))

            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            config.attributes["version_table_schema"] = schema

            logger.info("migrate: running alembic upgrade -> head (schema=%s)", schema)
            command.upgrade(config, "head")
            logger.info("migrate: upgrade complete")

            if apply_grants:
                _apply_grants(connection, schema)
            else:
                logger.info("grants: skipped (--apply-grants not set)")

            connection.commit()
    finally:
        engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Apply code search schema migrations.")
    parser.add_argument(
        "--apply-grants",
        action="store_true",
        default=False,
        help="Also apply least-privilege grants to APP_SP_ROLE / JOB_WRITER_ROLE (opt-in).",
    )
    args = parser.parse_args()
    run(apply_grants=args.apply_grants)


if __name__ == "__main__":
    main()
