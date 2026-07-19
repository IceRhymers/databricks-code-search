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
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, text

from app.db.client import create_db_engine
from app.db.grants import build_app_grants, build_job_grants, quote_ident, validate_role

logger = logging.getLogger("migrate")

# Core and gated-semantic revision locations, joined with os.pathsep for the semantic
# Config only (alembic.ini declares path_separator=os). Kept OUT of alembic.ini so the
# default core path never sees the semantic branch.
_CORE_VERSIONS = "app/alembic/versions"
_SEMANTIC_VERSIONS = "app/alembic/versions_semantic"


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


_ACK_ENV = "CODE_SEARCH_SEMANTIC_ACK"
_ACK_PHRASE = "enable-semantic"


def _require_semantic_ack() -> None:
    """Refuse to run the gated DDL without an explicit, deliberate acknowledgement.

    The guard lives HERE rather than only in the ``migrate-semantic`` Make recipe
    because the recipe is not the only way in: the runbook prints the underlying
    ``scripts/migrate.py --semantic`` command, and CI/automation can invoke it
    directly. A Makefile-only prompt would leave the irreversible-prerequisite
    warning trivially bypassable, which is exactly what AC-3 is meant to prevent.

    Two accepted forms: ``CODE_SEARCH_SEMANTIC_ACK=1`` for non-interactive callers
    (what the Make recipe sets after its own typed confirm), or an interactive typed
    confirmation on a TTY. A non-TTY caller with no ack aborts rather than silently
    proceeding.
    """
    if os.environ.get(_ACK_ENV) == "1":
        return
    if sys.stdin.isatty():
        print(
            "!! migrate-semantic creates 'chunks' using the BETA lakebase_vector/"
            "lakebase_text extensions.\n"
            "!! PREREQUISITE (irreversible, project-level, out-of-band): this Lakebase "
            "project's Databricks-managed shared_preload_libraries MUST already include "
            "lakebase_vector,lakebase_text.\n"
            "!! See docs/runbooks/semantic-enablement.md."
        )
        if input(f'Type "{_ACK_PHRASE}" to proceed: ').strip() == _ACK_PHRASE:
            return
        raise RuntimeError("semantic migrate aborted at the confirmation prompt")
    raise RuntimeError(
        f"semantic migrate requires an explicit acknowledgement: set {_ACK_ENV}=1 for a "
        "non-interactive caller, or run `make migrate-semantic TARGET=...` on a terminal. "
        "See docs/runbooks/semantic-enablement.md."
    )


def run_semantic() -> None:
    """Apply the GATED semantic revision into a SEPARATE version table.

    This path is deliberately isolated from :func:`run` (the core migrate). It records
    the semantic head in ``alembic_version_semantic`` -- NEVER in the core
    ``alembic_version`` -- so a later core ``upgrade head`` can't hit an unknown
    ``0002sem`` and break every future core migration (the C1 invariant). The
    ``version_locations`` override lives here, never in ``alembic.ini``, for the same
    reason: the default core path must never see the semantic branch.

    Ordering vs 0001 is NOT enforced by a graph edge (a separate version table can't
    see 0001 as applied). Instead this pre-checks that ``files`` exists -- the real
    invariant the ``chunks`` FK needs -- and aborts loudly if it does not. This does
    NOT run the managed ``shared_preload_libraries`` change; the gated revision's
    ``CREATE EXTENSION`` fails loudly if that irreversible prerequisite is absent.
    """
    _require_semantic_ack()  # before any connection: no side effects without consent
    engine = create_db_engine()
    try:
        with engine.connect() as connection:
            schema = _resolve_schema(connection)
            connection.execute(text(f"SET search_path TO {quote_ident(schema)}, public"))

            # Pre-check: the chunks FK REFERENCES files(id), so files must already exist
            # (core migrate must have run first). to_regclass returns NULL when absent.
            files_rel = connection.execute(text("SELECT to_regclass('files')")).scalar()
            if files_rel is None:
                raise RuntimeError(
                    f"semantic migrate: table 'files' not found in schema {schema!r}; run the "
                    "core migrate (make migrate) FIRST -- the chunks FK depends on it."
                )

            config = Config("alembic.ini")
            config.set_main_option(
                "version_locations", os.pathsep.join([_CORE_VERSIONS, _SEMANTIC_VERSIONS])
            )
            config.attributes["connection"] = connection
            config.attributes["version_table_schema"] = schema
            # The load-bearing isolation: the semantic head lands in its OWN table.
            config.attributes["version_table"] = "alembic_version_semantic"

            logger.info(
                "migrate-semantic: running alembic upgrade -> semantic@head (schema=%s)", schema
            )
            command.upgrade(config, "semantic@head")
            logger.info("migrate-semantic: upgrade complete")

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
    parser.add_argument(
        "--semantic",
        action="store_true",
        default=False,
        help="Apply the GATED semantic revision into the separate alembic_version_semantic "
        "table (issue #14). Requires the beta lakebase_* extensions to be reachable.",
    )
    args = parser.parse_args()
    if args.semantic:
        if args.apply_grants:
            parser.error("--semantic does not apply grants; re-grant separately (see runbook)")
        run_semantic()
        return
    run(apply_grants=args.apply_grants)


if __name__ == "__main__":
    main()
