"""Static YAML checks for resources/job.yml's one-logical-corpus-writer pin.

Pure source-level check (parse the checked-in YAML, no `databricks` CLI, no
network, no bundle variable resolution) so it runs in `make test` and stays
fast. The DAB-resolved values (after `${var.*}` substitution, per-target
overlays) are covered separately in
`tests/integration/test_job_resource_resolved.py`, which needs an
authenticated `databricks` CLI and is skip-guarded when one isn't available.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JOB_RESOURCE_PATH = _REPO_ROOT / "resources" / "job.yml"


def _load_code_search_index_job() -> dict[str, Any]:
    raw = yaml.safe_load(_JOB_RESOURCE_PATH.read_text())
    return raw["resources"]["jobs"]["code_search_index"]


@pytest.mark.unit
def test_max_concurrent_runs_is_pinned_to_one() -> None:
    """Global desired-state reconciliation (#56) needs at most one run writing the
    corpus at a time -- see indexer/job.py's module docstring and
    docs/runbooks/indexing-parallelism.md §1.1 for the full invariant."""
    job = _load_code_search_index_job()
    assert job["max_concurrent_runs"] == 1


@pytest.mark.unit
def test_queueing_is_preserved() -> None:
    """The pin only stays harmless to throughput because an overlapping trigger
    queues instead of being dropped."""
    job = _load_code_search_index_job()
    assert job["queue"]["enabled"] is True


@pytest.mark.unit
def test_existing_job_wiring_is_untouched() -> None:
    """The pin must not alter the task/environment/schedule wiring this job already
    depends on (python_wheel_task entry point, serverless environment, cron)."""
    job = _load_code_search_index_job()
    assert job["name"] == "code-search-index"
    tasks = job["tasks"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task["python_wheel_task"]["package_name"] == "databricks-code-search"
    assert task["python_wheel_task"]["entry_point"] == "code-search-index"
    assert "schedule" in job
    assert job["schedule"]["quartz_cron_expression"] == "${var.index_schedule}"
