"""Lakebase Search deploy preflight: probe that the target project has it enabled.

Semantic search is default-on, and migration ``0004``'s ``CREATE EXTENSION
lakebase_vector`` / ``lakebase_text`` fails loudly ("must be loaded via
shared_preload_libraries") when Lakebase Search is not enabled on the project. That
error is the intended signal, but it surfaces mid-deploy inside Alembic. This preflight
runs the same check up front, so a deploy stops BEFORE ``make migrate`` with the exact
remedy, rather than partway through the migration chain.

Enabling Lakebase Search is (as of 2026-10) NOT exposed through the Lakebase project
API / SDK / bundle -- ``databricks.sdk.service.postgres.ProjectSpec`` carries no search
flag -- so this script probes and instructs; it cannot enable. It is deliberately
structured so that if the REST API later grows an enable field, the probe body in
:func:`main` is the single place to swap for an enable call.

Probe: ``pg_available_extensions`` lists an extension only when the server CAN install
it, which is exactly what enabling Lakebase Search controls. Both of
``lakebase_vector`` and ``lakebase_text`` must be present.

Exit codes: 0 = enabled (probe found both); 1 = not enabled (prints the remedy).
Connection/env handling mirrors ``scripts/migrate.py``: the engine comes from
:func:`app.db.client.create_db_engine`, driven by ``LAKEBASE_ENDPOINT`` /
``LAKEBASE_DATABASE`` (the Makefile/deploy.sh resolve both from bundle-validate JSON).
No Databricks SDK import happens at module scope.
"""

from __future__ import annotations

import logging
import sys

from sqlalchemy import text

from app.db.client import create_db_engine

_SEARCH_EXTENSIONS = ("lakebase_vector", "lakebase_text")
_DOCS_URL = "https://docs.databricks.com/aws/en/oltp/projects/lakebase-search"


def _available_extensions() -> set[str]:
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name FROM pg_available_extensions WHERE name = ANY(:names)"),
                {"names": list(_SEARCH_EXTENSIONS)},
            ).scalars()
            return set(rows)
    finally:
        engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="deploy-preflight: %(message)s")
    available = _available_extensions()
    missing = [e for e in _SEARCH_EXTENSIONS if e not in available]
    if not missing:
        logging.info("Lakebase Search is enabled (found: %s)", ", ".join(sorted(available)))
        return 0

    print(  # noqa: T201 — operator-facing instructions, not a log line
        "\n".join(
            [
                "",
                "deploy: ERROR Lakebase Search is NOT enabled on this Lakebase project.",
                f"deploy:   missing extension(s): {', '.join(missing)}",
                "deploy:",
                "deploy:   Enable it (self-serve, irreversible, restarts the project's computes):",
                "deploy:     1. Open the Lakebase project in the Databricks workspace UI.",
                "deploy:     2. Settings -> Lakebase Search -> Enable Lakebase Search.",
                "deploy:     3. Re-run this deploy once the project's computes are back.",
                "deploy:",
                f"deploy:   Docs: {_DOCS_URL}",
                "deploy:   See docs/runbooks/semantic-enablement.md (section 1).",
                "",
            ]
        ),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
