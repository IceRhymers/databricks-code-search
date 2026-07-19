"""Unit tests for scripts/ci_branch.py (ephemeral Lakebase CI branch lifecycle).

The SDK is faked throughout, so these never touch a workspace. Two properties are
load-bearing and pinned here: teardown must NEVER raise (a purge failure would
otherwise mask the test failure that CI actually needs to report), and the branch
must be created with an expiry (the API rejects a branch without one, and the TTL is
what stops a cancelled run from leaking a branch).
"""

from __future__ import annotations

from typing import Any

import pytest

import scripts.ci_branch as ci_branch


class _Op:
    def wait(self) -> None:
        return None


class _Status:
    def __init__(self, endpoint_type: str) -> None:
        self.endpoint_type = endpoint_type


class _Endpoint:
    def __init__(self, name: str, endpoint_type: str) -> None:
        self.name = name
        self.status = _Status(endpoint_type)


class _FakePostgres:
    def __init__(self, endpoints: list[_Endpoint], *, delete_raises: bool = False) -> None:
        self._endpoints = endpoints
        self._delete_raises = delete_raises
        self.created: dict[str, Any] = {}
        self.deleted: list[tuple[str, bool]] = []

    def create_branch(self, *, parent: str, branch_id: str, branch: Any, **kw: Any) -> _Op:
        self.created = {"parent": parent, "branch_id": branch_id, "branch": branch, **kw}
        return _Op()

    def list_endpoints(self, parent: str) -> list[_Endpoint]:
        return self._endpoints

    def delete_branch(self, name: str, *, purge: bool = False) -> _Op:
        if self._delete_raises:
            raise RuntimeError("transient API failure")
        self.deleted.append((name, purge))
        return _Op()


class _FakeClient:
    def __init__(self, postgres: _FakePostgres) -> None:
        self.postgres = postgres


@pytest.mark.unit
def test_branch_path_is_the_qualified_resource_path() -> None:
    assert ci_branch.branch_path("code-search-ci", "pr-1") == (
        "projects/code-search-ci/branches/pr-1"
    )


@pytest.mark.unit
def test_up_forks_production_with_a_ttl_and_returns_the_rw_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgapi = _FakePostgres(
        [
            _Endpoint("projects/p/branches/b/endpoints/ro", "EndpointType.ENDPOINT_TYPE_READ_ONLY"),
            _Endpoint(
                "projects/p/branches/b/endpoints/primary",
                "EndpointType.ENDPOINT_TYPE_READ_WRITE",
            ),
        ]
    )
    monkeypatch.setattr(ci_branch, "_client", lambda: _FakeClient(pgapi))

    env = ci_branch.up("code-search-ci", "pr-1", ttl_seconds=60)

    # Picks the READ_WRITE endpoint, not merely the first one.
    assert env["LAKEBASE_ENDPOINT"] == "projects/p/branches/b/endpoints/primary"
    assert env["LAKEBASE_DATABASE"] == "databricks_postgres"

    spec = pgapi.created["branch"].spec
    assert spec.source_branch == "projects/code-search-ci/branches/production"
    # An expiry is REQUIRED by the API, and is the backstop against leaking a branch
    # when a run is cancelled before teardown.
    assert spec.ttl.seconds == 60
    # A re-run of the same workflow must replace its own branch, not collide.
    assert pgapi.created["replace_existing"] is True


@pytest.mark.unit
def test_up_raises_when_the_branch_has_no_read_write_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pgapi = _FakePostgres(
        [_Endpoint("projects/p/branches/b/endpoints/ro", "EndpointType.ENDPOINT_TYPE_READ_ONLY")]
    )
    monkeypatch.setattr(ci_branch, "_client", lambda: _FakeClient(pgapi))
    with pytest.raises(RuntimeError, match="no READ_WRITE endpoint"):
        ci_branch.up("code-search-ci", "pr-1", ttl_seconds=60)


@pytest.mark.unit
def test_down_purges_the_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    pgapi = _FakePostgres([])
    monkeypatch.setattr(ci_branch, "_client", lambda: _FakeClient(pgapi))
    ci_branch.down("code-search-ci", "pr-1")
    assert pgapi.deleted == [("projects/code-search-ci/branches/pr-1", True)]


@pytest.mark.unit
def test_down_never_raises_so_teardown_cannot_mask_a_test_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A purge failure must degrade to a warning: the TTL still reclaims the branch,
    and surfacing it as an error would replace the real CI failure in the log tail."""
    pgapi = _FakePostgres([], delete_raises=True)
    monkeypatch.setattr(ci_branch, "_client", lambda: _FakeClient(pgapi))
    ci_branch.down("code-search-ci", "pr-1")  # must not raise
    assert "ttl will reclaim it" in caplog.text
