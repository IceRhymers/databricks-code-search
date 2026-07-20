"""Pure, side-effect-free builders for least-privilege PostgreSQL grants.

These functions only assemble SQL strings; execution lives in
``scripts/migrate.py``. Every schema/role identifier is validated against a
strict allowlist and quoted via psycopg so hostile input cannot inject SQL.

The application role receives read-only access; the job role receives
read-write access (SELECT/INSERT/UPDATE/DELETE + sequence usage) but no DDL.
Mapping a Databricks client-id to a concrete role is deferred to issue #6.

``SELECT`` is first-order for the job role, not incidental: the indexing job's
pre-fan-out stamp read (``indexer.job._read_stamps``) issues a plain ``SELECT``
against ``repos``, and ``indexer.store`` relies on ``RETURNING`` and filtered
``DELETE``. Granting it explicitly means the job no longer depends on the
privilege arriving by some undocumented route. Adding it makes the deploy
grant-coupled: an existing deployment needs a ``deploy.sh --apply-grants``
re-run, not a schema-only migrate.
"""

from __future__ import annotations

import re

import psycopg

_ROLE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_role(name: str) -> str:
    """Return ``name`` unchanged if it is a safe identifier, else raise.

    A valid identifier matches ``^[A-Za-z0-9_-]+$`` and is 1..63 characters
    long (PostgreSQL's identifier length limit).
    """
    if not 1 <= len(name) <= 63:
        raise ValueError(f"identifier must be 1..63 characters, got length {len(name)}: {name!r}")
    if not _ROLE_RE.match(name):
        raise ValueError(
            f"identifier must match {_ROLE_RE.pattern} (letters, digits, '_', '-'): {name!r}"
        )
    return name


def quote_ident(name: str) -> str:
    """Validate ``name`` then return it as a safely double-quoted identifier."""
    validate_role(name)
    return psycopg.sql.Identifier(name).as_string(None)


def build_app_grants(schema: str, app_role: str) -> list[str]:
    """Build read-only grants for the application role on ``schema``."""
    s = quote_ident(schema)
    r = quote_ident(app_role)
    return [
        f"GRANT USAGE ON SCHEMA {s} TO {r}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO {r}",
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT SELECT ON TABLES TO {r}",
    ]


def build_job_grants(schema: str, job_role: str) -> list[str]:
    """Build write grants (no DDL) for the indexer job role on ``schema``."""
    s = quote_ident(schema)
    r = quote_ident(job_role)
    return [
        f"GRANT USAGE ON SCHEMA {s} TO {r}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {s} TO {r}",
        f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {s} TO {r}",
        (
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {r}"
        ),
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} GRANT USAGE ON SEQUENCES TO {r}",
    ]
