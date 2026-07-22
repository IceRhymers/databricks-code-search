"""DAB-resolved checks for resources/job.yml's one-logical-corpus-writer pin.

tests/unit/test_job_resource.py checks the checked-in YAML source; this module
checks what `databricks bundle validate` actually RESOLVES `code_search_index`
to per target, after `${var.*}` substitution and any per-target resource
overlay (see `databricks.yml`'s `targets.prod.resources.jobs.code_search_index`
`run_as` overlay, which only widens the resolved job, never touches
`max_concurrent_runs`/`queue`).

Needs an authenticated `databricks` CLI reaching a real workspace -- skipped,
not failed, when the CLI is missing or validation cannot authenticate/reach a
workspace, so a laptop or sandbox without Databricks credentials does not fail
`make test-integration` spuriously (same skip-guard precedent as
`tests/unit/test_semantics_version_tripwire.py`'s git checks).
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DATABRICKS_CLI = shutil.which("databricks")

# prod declares job_run_as_sp with an empty default (see databricks.yml) precisely so
# `validate -t dev` stays green without it; prod validation therefore needs a
# placeholder value here. It only affects the job's `run_as` block, never
# `max_concurrent_runs`/`queue`.
_PROD_PLACEHOLDER_SP = "00000000-0000-0000-0000-000000000000"


@functools.lru_cache(maxsize=1)
def _skip_reason_if_unauthenticated() -> str | None:
    """Probe auth/CLI availability independently of `bundle validate`.

    This is the ONLY thing allowed to skip the tests below. `bundle validate`
    itself is deliberately never treated as skippable on failure: var
    resolution and the prod `run_as` overlay are exactly what this module
    exists to catch, and a test that skips on any non-zero `validate` exit
    would also skip straight through a real bundle-config regression. A cheap,
    bundle-independent call (`current-user me`) distinguishes "no/expired
    auth or unreachable workspace" (skip) from everything else (let it fail).
    Cached (once per test run) since every test in this module would otherwise
    repeat the same round trip.
    """
    if _DATABRICKS_CLI is None:
        return "databricks CLI not found on PATH"
    try:
        proc = subprocess.run(
            [_DATABRICKS_CLI, "current-user", "me", "-o", "json"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not run `databricks current-user me`: {exc}"
    if proc.returncode != 0:
        return (
            "`databricks current-user me` failed (no/expired auth or unreachable "
            f"workspace): {proc.stderr.strip()[:500]}"
        )
    return None


def _bundle_validate(target: str, *, extra_args: list[str] | None = None) -> dict[str, Any]:
    """Run `databricks bundle validate -t <target> -o json` and return the parsed doc.

    Skips only via `_skip_reason_if_unauthenticated`'s bundle-independent probe.
    Once auth is confirmed working, everything below is a real assertion: a
    non-zero exit, a timeout, or non-JSON stdout all fail the test rather than
    skip it, since at that point the failure is this repo's bundle config, not
    the environment. stdout/stderr are captured separately -- `bundle validate`
    writes advisory warnings (e.g. unmatched sync globs) to stderr even on
    success, and merging them into stdout would break JSON parsing.
    """
    skip_reason = _skip_reason_if_unauthenticated()
    if skip_reason is not None:
        pytest.skip(skip_reason)

    assert _DATABRICKS_CLI is not None  # guaranteed by the probe above
    cmd = [_DATABRICKS_CLI, "bundle", "validate", "-t", target, "-o", "json", *(extra_args or [])]
    proc = subprocess.run(
        cmd,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"`databricks bundle validate -t {target}` failed (auth already confirmed working, "
        f"so this is a real bundle-config problem): {proc.stderr.strip()[:1000]}"
    )
    return json.loads(proc.stdout)


def _resolved_code_search_index_job(target: str, **kw: Any) -> dict[str, Any]:
    doc = _bundle_validate(target, **kw)
    return doc["resources"]["jobs"]["code_search_index"]


@pytest.mark.integration
def test_dev_resolves_max_concurrent_runs_to_one() -> None:
    job = _resolved_code_search_index_job("dev")
    assert job["max_concurrent_runs"] == 1
    assert job["queue"]["enabled"] is True


@pytest.mark.integration
def test_prod_resolves_max_concurrent_runs_to_one() -> None:
    """Prod applies databricks.yml's `run_as` overlay for `code_search_index` (the
    job runs as the pre-created writer SP) -- assert the overlay does not
    disturb the pinned concurrency/queueing it was layered on top of."""
    job = _resolved_code_search_index_job(
        "prod", extra_args=[f"--var=job_run_as_sp={_PROD_PLACEHOLDER_SP}"]
    )
    assert job["max_concurrent_runs"] == 1
    assert job["queue"]["enabled"] is True
    assert job["run_as"]["service_principal_name"] == _PROD_PLACEHOLDER_SP
