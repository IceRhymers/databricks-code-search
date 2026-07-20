"""Unit tests for indexer.fetch using httpx.MockTransport + in-memory tarballs."""

from __future__ import annotations

import io
import logging
import tarfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import indexer.fetch as fetch
from indexer.fetch import (
    RateLimitError,
    RepoMeta,
    assert_disk_headroom,
    download_tarball,
    extract_tarball,
    list_org_repos,
    list_user_repos,
    resolve_ref,
)

ORG = "acme"
REPO = "widgets"
BRANCH = "main"
SHA = "abc1234def5678"
TOP_DIR = f"{ORG}-{REPO}-{SHA[:7]}"
CODELOAD_URL = "https://codeload.github.com/acme/widgets/tar.gz/abc1234"


def _make_tarball(names_to_content: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in names_to_content.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


CLEAN_TARBALL = _make_tarball(
    {
        f"{TOP_DIR}/README.md": b"# hello\n",
        f"{TOP_DIR}/src/main.py": b"def f():\n    return 1\n",
    }
)


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/repos/{ORG}/{REPO}":
        return httpx.Response(200, json={"default_branch": BRANCH})
    if path == f"/repos/{ORG}/{REPO}/commits/{BRANCH}":
        return httpx.Response(200, json={"sha": SHA})
    if path == f"/repos/{ORG}/{REPO}/tarball/{SHA}":
        return httpx.Response(302, headers={"location": CODELOAD_URL})
    if request.url == httpx.URL(CODELOAD_URL):
        return httpx.Response(200, content=CLEAN_TARBALL)
    return httpx.Response(404)


def _client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(_handler))


@pytest.mark.unit
def test_resolve_ref_returns_branch_and_sha() -> None:
    with _client() as client:
        assert resolve_ref(client, ORG, REPO) == (BRANCH, SHA)


@pytest.mark.unit
def test_download_tarball_follows_redirect_and_writes_bytes(tmp_path: Path) -> None:
    with _client() as client:
        out = download_tarball(client, ORG, REPO, SHA, tmp_path)
    assert out == tmp_path / "source.tar.gz"
    assert out.read_bytes() == CLEAN_TARBALL


@pytest.mark.unit
def test_download_tarball_rejects_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cap below the served tarball size -> the streamed download aborts.
    monkeypatch.setattr(fetch, "MAX_TARBALL_BYTES", 4)
    with _client() as client:
        with pytest.raises(ValueError, match="exceeds"):
            download_tarball(client, ORG, REPO, SHA, tmp_path)


@pytest.mark.unit
def test_extract_tarball_rejects_decompression_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cap below the summed member size -> extraction is refused before writing.
    monkeypatch.setattr(fetch, "MAX_EXTRACTED_BYTES", 4)
    tar_path = tmp_path / "source.tar.gz"
    tar_path.write_bytes(CLEAN_TARBALL)
    dest = tmp_path / "extracted"
    with pytest.raises(ValueError, match="exceeding"):
        extract_tarball(tar_path, dest)
    assert not any(dest.iterdir()) if dest.exists() else True


@pytest.mark.unit
def test_extract_tarball_yields_top_level_dir(tmp_path: Path) -> None:
    tar_path = tmp_path / "source.tar.gz"
    tar_path.write_bytes(CLEAN_TARBALL)
    root = extract_tarball(tar_path, tmp_path / "extracted")
    assert root.name == TOP_DIR
    assert (root / "README.md").read_text() == "# hello\n"
    assert (root / "src" / "main.py").exists()


@pytest.mark.unit
def test_extract_tarball_neutralizes_path_traversal(tmp_path: Path) -> None:
    malicious = _make_tarball(
        {
            f"{TOP_DIR}/ok.py": b"x = 1\n",
            "../evil.txt": b"pwned\n",
        }
    )
    tar_path = tmp_path / "evil.tar.gz"
    tar_path.write_bytes(malicious)
    dest = tmp_path / "extracted"

    # The data filter blocks the escaping member; nothing is written outside dest.
    with pytest.raises(tarfile.OutsideDestinationError):
        extract_tarball(tar_path, dest)
    assert not (dest.parent / "evil.txt").exists()


# --- Enumeration (AC 14-21) -------------------------------------------------


def _repo_json(
    full_name: str, *, fork: bool = False, archived: bool = False, size: int = 1
) -> dict[str, object]:
    return {"full_name": full_name, "fork": fork, "archived": archived, "size": size}


def _recording_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[httpx.Client, list[httpx.Request]]:
    """A client whose every outbound request is appended to the returned list."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(record)), seen


@pytest.mark.unit
def test_list_org_repos_hits_org_endpoint_with_pagination_params() -> None:
    """AC 14: /orgs/{org}/repos with parsed per_page=100 and page=1."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_repo_json("acme/widgets")])

    client, seen = _recording_client(handler)
    with client:
        repos = list_org_repos(client, ORG)

    assert [r.full_name for r in repos] == ["acme/widgets"]
    assert len(seen) == 1
    assert seen[0].url.path == "/orgs/acme/repos"
    # Assert PARSED params — ordering is not part of the contract.
    assert seen[0].url.params["per_page"] == "100"
    assert seen[0].url.params["page"] == "1"


@pytest.mark.unit
def test_list_user_repos_hits_user_endpoint_with_pagination_params() -> None:
    """AC 15: /users/{user}/repos with the same parsed params."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_repo_json("u/thing")])

    client, seen = _recording_client(handler)
    with client:
        repos = list_user_repos(client, "u")

    assert [r.full_name for r in repos] == ["u/thing"]
    assert len(seen) == 1
    assert seen[0].url.path == "/users/u/repos"
    assert seen[0].url.params["per_page"] == "100"
    assert seen[0].url.params["page"] == "1"


@pytest.mark.unit
def test_list_repos_follows_link_rel_next() -> None:
    """AC 16: 100 + Link rel=next, then 50 without -> 150 repos in 2 requests."""
    page1 = [_repo_json(f"acme/r{i}") for i in range(100)]
    page2 = [_repo_json(f"acme/r{i}") for i in range(100, 150)]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return httpx.Response(
                200,
                json=page1,
                headers={
                    "Link": '<https://api.github.com/orgs/acme/repos?page=2>; rel="next", '
                    '<https://api.github.com/orgs/acme/repos?page=2>; rel="last"'
                },
            )
        return httpx.Response(200, json=page2)

    client, seen = _recording_client(handler)
    with client:
        repos = list_org_repos(client, ORG)

    assert len(repos) == 150
    assert len(seen) == 2
    assert seen[1].url.params["page"] == "2"


@pytest.mark.unit
def test_list_repos_terminates_without_rel_next() -> None:
    """AC 17: a Link header carrying only prev/last stops after one request."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_repo_json("acme/widgets")],
            headers={
                "Link": '<https://api.github.com/orgs/acme/repos?page=1>; rel="prev", '
                '<https://api.github.com/orgs/acme/repos?page=3>; rel="last"'
            },
        )

    client, seen = _recording_client(handler)
    with client:
        repos = list_org_repos(client, ORG)

    assert len(repos) == 1
    assert len(seen) == 1


@pytest.mark.unit
def test_list_repos_propagates_404() -> None:
    """AC 18: a 404 stays an ordinary HTTPStatusError (raise_for_status convention)."""
    client, _ = _recording_client(lambda request: httpx.Response(404))
    with client:
        with pytest.raises(httpx.HTTPStatusError):
            list_org_repos(client, "nope")


@pytest.mark.unit
def test_repo_meta_maps_github_fields() -> None:
    """AC 19: full_name/fork/archived/size_kb map from full_name/fork/archived/size."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[_repo_json("acme/widgets", fork=True, archived=True, size=1234)],
        )

    client, _ = _recording_client(handler)
    with client:
        repos = list_org_repos(client, ORG)

    assert repos == [RepoMeta(full_name="acme/widgets", fork=True, archived=True, size_kb=1234)]


@pytest.mark.unit
def test_rate_limit_429_raises_rate_limit_error() -> None:
    """AC 20a: 429 is always a RateLimitError, naming the selector."""
    client, _ = _recording_client(lambda request: httpx.Response(429))
    with client:
        with pytest.raises(RateLimitError, match="acme"):
            list_org_repos(client, ORG)


@pytest.mark.unit
def test_rate_limit_403_with_retry_after_raises_rate_limit_error() -> None:
    """AC 20b: 403 + Retry-After -> RateLimitError quoting the derived wait."""
    client, _ = _recording_client(
        lambda request: httpx.Response(403, headers={"Retry-After": "60"})
    )
    with client:
        with pytest.raises(RateLimitError) as excinfo:
            list_org_repos(client, ORG)

    message = str(excinfo.value)
    assert "60" in message
    assert ORG in message


@pytest.mark.unit
def test_rate_limit_403_with_zero_remaining_quotes_reset_time() -> None:
    """AC 20c: 403 + X-RateLimit-Remaining: 0 -> RateLimitError from X-RateLimit-Reset."""
    client, _ = _recording_client(
        lambda request: httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1700000000"},
        )
    )
    with client:
        with pytest.raises(RateLimitError) as excinfo:
            list_org_repos(client, ORG)

    message = str(excinfo.value)
    assert "2023-11-14T22:13:20Z" in message
    assert ORG in message


@pytest.mark.unit
def test_bare_403_is_http_status_error_not_rate_limit() -> None:
    """AC 20d: the org-scope permission failure must NOT be mislabeled as a quota failure."""
    client, _ = _recording_client(
        lambda request: httpx.Response(403, headers={"X-RateLimit-Remaining": "4999"})
    )
    with client:
        with pytest.raises(httpx.HTTPStatusError):
            list_org_repos(client, ORG)


@pytest.mark.unit
def test_retry_after_takes_precedence_over_reset() -> None:
    """AC 20: with both headers present, Retry-After wins."""
    client, _ = _recording_client(
        lambda request: httpx.Response(
            403,
            headers={
                "Retry-After": "42",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": "1700000000",
            },
        )
    )
    with client:
        with pytest.raises(RateLimitError) as excinfo:
            list_org_repos(client, ORG)

    message = str(excinfo.value)
    assert "42" in message
    assert "2023-11-14T22:13:20Z" not in message


@pytest.mark.unit
def test_rate_limit_remaining_logged_once_per_selector(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC 21: two selectors -> exactly two INFO records, each with its own remaining.

    The org selector deliberately spans TWO pages. With single-page selectors a
    per-page implementation would also emit exactly two records and pass; three
    pages total means a per-page logger emits three and the count assertion fails.
    """
    remaining_by_selector = {"acme": "4321", "u": "1234"}

    def handler(request: httpx.Request) -> httpx.Response:
        selector = request.url.path.split("/")[2]
        headers = {"X-RateLimit-Remaining": remaining_by_selector[selector]}
        if selector == "acme" and request.url.params["page"] == "1":
            headers["Link"] = f'<{request.url}&page=2>; rel="next"'
        return httpx.Response(
            200,
            json=[_repo_json(f"{selector}/thing")],
            headers=headers,
        )

    client, seen = _recording_client(handler)
    with caplog.at_level(logging.INFO, logger="indexer.fetch"):
        with client:
            list_org_repos(client, "acme")
            list_user_repos(client, "u")

    # 3 HTTP pages, 2 log records: the record is per SELECTOR, not per page.
    assert len(seen) == 3
    records = [r for r in caplog.records if r.name == "indexer.fetch"]
    assert len(records) == 2
    assert "acme" in records[0].getMessage() and "4321" in records[0].getMessage()
    assert "u" in records[1].getMessage() and "1234" in records[1].getMessage()


@pytest.mark.unit
def test_size_cap_error_names_overage_and_config_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 43: the size-cap ValueError carries the byte-exact overage and exclude.size_mb."""
    monkeypatch.setattr(fetch, "MAX_TARBALL_BYTES", 4)
    with _client() as client:
        with pytest.raises(ValueError) as excinfo:
            download_tarball(client, ORG, REPO, SHA, tmp_path)

    message = str(excinfo.value)
    assert "exclude.size_mb" in message
    # The stream aborts on the first chunk, so the overage is that chunk minus the cap.
    assert f"by {len(CLEAN_TARBALL) - 4} bytes" in message


# --- disk headroom guard ----------------------------------------------------


@pytest.mark.unit
def test_required_free_bytes_sums_both_caps() -> None:
    """Both caps are alive at once, so they SUM.

    The tarball stays on disk inside the worker's TemporaryDirectory while the
    extracted tree grows beside it; a max() here would under-reserve by 500 MB
    per worker and silently reintroduce the failure the guard exists to prevent.
    """
    assert fetch.REQUIRED_FREE_BYTES == fetch.MAX_TARBALL_BYTES + fetch.MAX_EXTRACTED_BYTES


@pytest.mark.unit
def test_assert_disk_headroom_passes_when_space_is_sufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        fetch.shutil, "disk_usage", lambda _p: _Usage(free=fetch.REQUIRED_FREE_BYTES)
    )
    # Exactly at the threshold must pass: the comparison is `<`, not `<=`.
    assert assert_disk_headroom(tmp_path, repo=f"{ORG}/{REPO}") is None


@pytest.mark.unit
def test_assert_disk_headroom_error_names_the_repo_and_the_config_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the guard is diagnosability.

    An opaque ENOSPC from inside tarfile says neither which of N concurrent
    workers overcommitted the disk nor what to change, so both are asserted.
    """
    monkeypatch.setattr(
        fetch.shutil, "disk_usage", lambda _p: _Usage(free=fetch.REQUIRED_FREE_BYTES - 1)
    )
    with pytest.raises(OSError) as excinfo:
        assert_disk_headroom(tmp_path, repo=f"{ORG}/{REPO}")

    message = str(excinfo.value)
    assert f"{ORG}/{REPO}" in message
    assert "index_concurrency" in message
    assert str(fetch.REQUIRED_FREE_BYTES) in message


class _Usage:
    """Stand-in for ``shutil.disk_usage``'s named tuple (only ``free`` is read)."""

    def __init__(self, *, free: int) -> None:
        self.total = 100_000_000_000
        self.used = self.total - free
        self.free = free
