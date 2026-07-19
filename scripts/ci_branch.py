"""Ephemeral Lakebase branch lifecycle for CI (issue #14 follow-up).

CI runs the integration suite against a REAL Lakebase branch rather than a local
``pgvector`` container, because the beta surface this repo depends on --
``lakebase_ann``'s ``<=>`` ordered-index path and ``lakebase_bm25``'s
``ts <@> to_bm25query(...)`` scorer -- does not exist in any Postgres image. A
stand-in can prove the fusion plumbing but never the ranking or the query plan
(see ``app/search/semantic.py::_leg_cte``).

A per-run BRANCH is the isolation primitive, not a shared database:

* Branches are copy-on-write forks, so creation is cheap and a run gets a private
  database it may freely ``CREATE``/``DROP`` in. The integration suite drops
  extensions and recreates tables; against a shared database, two concurrent PRs
  would corrupt each other.
* A branch auto-provisions its own READ_WRITE endpoint named ``primary``. Do NOT
  create one -- the API rejects a second read_write endpoint on the same branch.
* ``ttl`` is a hard safety net: if a workflow is cancelled between ``up`` and
  ``down``, the branch still expires on its own rather than leaking cost. The API
  REQUIRES an expiry (``expire_time``, ``ttl``, or ``no_expiry``) and ``ttl`` must
  be a protobuf ``Duration``, not a string.

Operational constraint (non-obvious): the CI project's ``production`` branch must
stay UN-MIGRATED. Forks inherit installed extensions, and
``test_no_vector_extension_installed`` asserts the core migration leaves no
vector-family extension installed. Keep the parent clean; each run's branch
installs whatever its test needs.

Usage:
    python scripts/ci_branch.py up   --project code-search-ci --branch pr-123
    python scripts/ci_branch.py down --project code-search-ci --branch pr-123

``up`` prints ``KEY=VALUE`` lines (``LAKEBASE_ENDPOINT``, ``LAKEBASE_DATABASE``)
suitable for ``>> "$GITHUB_ENV"``. ``down`` is idempotent and never fails the
build: a teardown error must not mask a real test failure.
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger("ci_branch")

_DEFAULT_DATABASE = "databricks_postgres"
# 2h: comfortably longer than any CI run, short enough that a leaked branch is
# self-correcting well inside a billing cycle.
_DEFAULT_TTL_SECONDS = 7200


def _client():  # type: ignore[no-untyped-def]
    """Lazy SDK import, mirroring app/db/client.py's seam."""
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def branch_path(project: str, branch: str) -> str:
    return f"projects/{project}/branches/{branch}"


def up(project: str, branch: str, *, ttl_seconds: int) -> dict[str, str]:
    """Fork ``production`` into an ephemeral branch and return its connection env."""
    from databricks.sdk.service import postgres as pg
    from google.protobuf.duration_pb2 import Duration

    w = _client()
    parent = f"projects/{project}"
    source = branch_path(project, "production")
    target = branch_path(project, branch)

    logger.info("creating branch %s (fork of %s, ttl=%ss)", target, source, ttl_seconds)
    w.postgres.create_branch(
        parent=parent,
        branch_id=branch,
        branch=pg.Branch(
            spec=pg.BranchSpec(
                source_branch=source,
                is_protected=False,
                # Proto Duration, not "7200s" -- a string raises AttributeError in the SDK.
                ttl=Duration(seconds=ttl_seconds),
            )
        ),
        # Re-running a workflow reuses the branch name; replace rather than collide.
        replace_existing=True,
    ).wait()

    # The branch auto-provisions its RW endpoint; resolve it instead of creating one.
    endpoints = list(w.postgres.list_endpoints(target))
    rw = next(
        (
            e
            for e in endpoints
            if str(getattr(e.status, "endpoint_type", "")).endswith("READ_WRITE")
        ),
        None,
    )
    if rw is None:
        raise RuntimeError(f"branch {target} has no READ_WRITE endpoint: {endpoints!r}")

    logger.info("branch ready; endpoint %s", rw.name)
    return {"LAKEBASE_ENDPOINT": rw.name, "LAKEBASE_DATABASE": _DEFAULT_DATABASE}


def down(project: str, branch: str) -> None:
    """Purge the ephemeral branch. Never raises -- teardown must not mask a test failure."""
    target = branch_path(project, branch)
    try:
        _client().postgres.delete_branch(target, purge=True).wait()
        logger.info("purged branch %s", target)
    except Exception as error:  # noqa: BLE001 - best-effort teardown
        # The ttl backstop still reclaims it, so a warning is the right severity.
        logger.warning("could not purge %s (%r); the branch ttl will reclaim it", target, error)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["up", "down"])
    parser.add_argument(
        "--project", required=True, help="Lakebase project id (e.g. code-search-ci)"
    )
    parser.add_argument("--branch", required=True, help="ephemeral branch id (e.g. pr-123)")
    parser.add_argument("--ttl-seconds", type=int, default=_DEFAULT_TTL_SECONDS)
    args = parser.parse_args()

    if args.action == "up":
        for key, value in up(args.project, args.branch, ttl_seconds=args.ttl_seconds).items():
            # Consumed via `>> "$GITHUB_ENV"`.
            sys.stdout.write(f"{key}={value}\n")
    else:
        down(args.project, args.branch)


if __name__ == "__main__":
    main()
