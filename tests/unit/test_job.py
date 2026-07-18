"""Unit tests for indexer.job: normalize_repo, read_github_token, orchestration.

Orchestration runs with every I/O boundary faked: a fake WorkspaceClient (secret
read), an httpx.MockTransport client (GitHub HTTP), a fake Engine/Connection, and
a recording ``index_fn`` — so no Databricks creds and no Postgres are needed.
"""

from __future__ import annotations

import base64
import io
import tarfile
from typing import Any

import httpx
import pytest

from indexer.job import normalize_repo, read_github_token, run
from indexer.languages import IndexCounts

# --- normalize_repo ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("acme/widgets", "acme/widgets"),
        ("https://github.com/acme/widgets", "acme/widgets"),
        ("https://github.com/acme/widgets.git", "acme/widgets"),
        ("git@github.com:acme/widgets.git", "acme/widgets"),
        ("  acme/widgets  ", "acme/widgets"),
        ("https://github.com/acme/widgets/", "acme/widgets"),
    ],
)
def test_normalize_repo_accepts(entry: str, expected: str) -> None:
    assert normalize_repo(entry) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry",
    [
        "",
        "   ",
        "acme",
        "acme/widgets/extra",
        "https://gitlab.com/acme/widgets",
        "git@bitbucket.org:acme/widgets.git",
        "https://evil.com/a/b",
        "../..",
        "acme/..",
        "../widgets",
        "https://github.com/acme/../secrets",
    ],
)
def test_normalize_repo_rejects(entry: str) -> None:
    with pytest.raises(ValueError):
        normalize_repo(entry)


# --- read_github_token ------------------------------------------------------


class _FakeSecret:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeSecrets:
    def __init__(self, value: str) -> None:
        self._value = value
        self.calls: list[tuple[str, str]] = []

    def get_secret(self, scope: str, key: str) -> _FakeSecret:
        self.calls.append((scope, key))
        return _FakeSecret(self._value)


class _FakeWorkspaceClient:
    def __init__(self, token: str) -> None:
        self.secrets = _FakeSecrets(base64.b64encode(token.encode()).decode())


@pytest.mark.unit
def test_read_github_token_decodes() -> None:
    wc = _FakeWorkspaceClient("ghp_secret123")
    assert read_github_token(wc, "scope", "key") == "ghp_secret123"
    assert wc.secrets.calls == [("scope", "key")]


# --- orchestration ----------------------------------------------------------


def _tarball(top_dir: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in {
            f"{top_dir}/main.py": b"def f():\n    return 1\n",
            f"{top_dir}/README.md": b"# hi\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _github_handler(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.strip("/").split("/")
    # /repos/{org}/{repo}...
    if len(parts) >= 3 and parts[0] == "repos":
        org, repo = parts[1], parts[2]
        if len(parts) == 3:
            return httpx.Response(200, json={"default_branch": "main"})
        if parts[3] == "commits":
            return httpx.Response(200, json={"sha": f"sha_{repo}"})
        if parts[3] == "tarball":
            return httpx.Response(200, content=_tarball(f"{org}-{repo}-shashas"))
    return httpx.Response(404)


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def connect(self) -> _FakeConn:
        return _FakeConn()

    def dispose(self) -> None:
        self.disposed = True


class _RecordingIndex:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.counts: list[IndexCounts] = []

    def __call__(
        self, conn: Any, *, name: str, default_branch: Any, head_sha: str, items: Any
    ) -> IndexCounts:
        materialized = list(items)
        self.calls.append(name)
        files = len(materialized)
        symbols = sum(len(syms) for _pf, syms in materialized)
        counts = IndexCounts(files=files, symbols=symbols, swept=0)
        self.counts.append(counts)
        return counts


def _run(repos: str, index_fn: Any) -> int:
    wc = _FakeWorkspaceClient("tok")
    engine = _FakeEngine()
    with httpx.Client(transport=httpx.MockTransport(_github_handler)) as client:
        return run(
            repos=repos,
            scope="s",
            key="k",
            endpoint="ep",
            database="db",
            workspace_client=wc,
            http_client=client,
            engine=engine,
            index_fn=index_fn,
        )


@pytest.mark.unit
def test_run_indexes_all_repos() -> None:
    idx = _RecordingIndex()
    code = _run("acme/widgets, acme/gadgets", idx)
    assert code == 0
    assert set(idx.calls) == {"acme/widgets", "acme/gadgets"}


@pytest.mark.unit
def test_run_parses_files_and_symbols() -> None:
    idx = _RecordingIndex()
    code = _run("acme/widgets", idx)
    assert code == 0
    # main.py + README.md both stored; main.py yields one function symbol.
    assert idx.calls == ["acme/widgets"]
    assert idx.counts == [IndexCounts(files=2, symbols=1, swept=0)]


@pytest.mark.unit
def test_run_isolates_failing_repo() -> None:
    idx = _RecordingIndex()
    # Second entry is an unsupported host -> normalize_repo raises inside the
    # per-repo try/except; the first still indexes and the exit code is 1.
    code = _run("acme/widgets, https://evil.com/a/b", idx)
    assert code == 1
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_run_empty_repos_is_noop() -> None:
    idx = _RecordingIndex()
    code = run(
        repos="   ",
        scope="s",
        key="k",
        endpoint="ep",
        database="db",
        workspace_client=None,
        http_client=None,
        engine=None,
        index_fn=idx,
    )
    assert code == 0
    assert idx.calls == []
