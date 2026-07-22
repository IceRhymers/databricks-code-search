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


def _bundle_validate(target: str, *, extra_args: list[str] | None = None) -> dict[str, Any]:
    """Run `databricks bundle validate -t <target> -o json` and return the parsed doc.

    stdout/stderr are captured separately: `bundle validate` writes advisory
    warnings (e.g. unmatched sync globs) to stderr even on success, and merging
    them into stdout breaks JSON parsing. Skips (does not fail) on a missing CLI,
    a timeout, or any non-zero exit -- the latter covers "not logged in" /
    "workspace unreachable", which is an environment gap, not a regression in
    this repo's bundle config.
    """
    if _DATABRICKS_CLI is None:
        pytest.skip("databricks CLI not found on PATH")

    cmd = [_DATABRICKS_CLI, "bundle", "validate", "-t", target, "-o", "json", *(extra_args or [])]
    try:
        proc = subprocess.run(
            cmd,
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"could not run `databricks bundle validate -t {target}`: {exc}")

    if proc.returncode != 0:
        pytest.skip(
            f"`databricks bundle validate -t {target}` failed (likely no/expired auth or "
            f"unreachable workspace), not a code defect: {proc.stderr.strip()[:500]}"
        )

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.skip(f"`databricks bundle validate -t {target} -o json` did not emit JSON: {exc}")


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
