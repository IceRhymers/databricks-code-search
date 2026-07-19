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

from app.config import Settings
from indexer.job import normalize_repo, read_github_token, run
from indexer.languages import IndexCounts, ParsedFile

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
        self.chunk_writer: Any = None

    def __call__(
        self,
        conn: Any,
        *,
        name: str,
        default_branch: Any,
        head_sha: str,
        items: Any,
        chunk_writer: Any = None,
    ) -> IndexCounts:
        materialized = list(items)
        self.calls.append(name)
        self.chunk_writer = chunk_writer
        files = len(materialized)
        symbols = sum(len(syms) for _pf, syms in materialized)
        counts = IndexCounts(files=files, symbols=symbols, swept=0)
        self.counts.append(counts)
        return counts


def _run(repos: str, index_fn: Any, *, cfg: Settings | None = None, embed_fn: Any = None) -> int:
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
            cfg=cfg,
            embed_fn=embed_fn,
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


# --- semantic chunk_writer wiring (issue #14 Phase 2) -----------------------
# Chunks are chunked+embedded OUTSIDE index_repo's transaction (A4): run()/
# _index_one() precompute a chunk_writer closure over already-embedded vectors
# and hand it to index_fn, which (in production) is index_repo.


class _FakeChunkConn:
    """Records execute() calls so write_chunks' delete-then-insert shape can be
    asserted without a real Postgres connection."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    def execute(self, stmt: Any, params: Any = None) -> Any:
        self.calls.append((stmt, params))
        return None


@pytest.mark.unit
def test_flag_off_passes_no_chunk_writer() -> None:
    idx = _RecordingIndex()
    code = _run("acme/widgets", idx, cfg=Settings(semantic_enabled=False))
    assert code == 0
    assert idx.chunk_writer is None


@pytest.mark.unit
def test_semantic_enabled_builds_and_wires_a_chunk_writer() -> None:
    idx = _RecordingIndex()
    embed_calls: list[list[str]] = []

    def fake_embed(texts: list[str]) -> list[list[float]]:
        embed_calls.append(list(texts))
        return [[0.5] for _ in texts]

    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    code = _run("acme/widgets", idx, cfg=cfg, embed_fn=fake_embed)
    assert code == 0
    assert idx.chunk_writer is not None
    # main.py + README.md's chunk text is embedded in one up-front call, not
    # lazily per file inside index_repo's transaction (A4).
    assert len(embed_calls) == 1
    assert len(embed_calls[0]) == 2

    # Exercise the closure directly: writing main.py's chunks is a
    # delete-then-insert against the chunks table, keyed by the given file_id.
    conn = _FakeChunkConn()
    pf = ParsedFile(path="main.py", lang="python", size=10, content="def f():\n    return 1\n")
    idx.chunk_writer(conn, 1, 42, pf)
    assert len(conn.calls) == 2
    delete_stmt, _ = conn.calls[0]
    assert delete_stmt.table.name == "chunks"
    insert_stmt, values = conn.calls[1]
    assert insert_stmt.table.name == "chunks"
    assert values == [
        {
            "file_id": 42,
            "chunk_index": 0,
            "content": "def f():\n    return 1\n",
            "embedding": [0.5],
        }
    ]


@pytest.mark.unit
def test_semantic_ceiling_exceeded_degrades_but_still_indexes_the_core() -> None:
    """A semantic failure must not cost the repo its CORE index.

    The semantic layer is additive. If the ceiling (or the embedder, or a dim/count
    mismatch) blows up, propagating would skip files/symbols AND the mark-and-sweep,
    leaving the repo silently stale -- strictly worse than stale chunks, which simply
    catch up on the next run.
    """
    idx = _RecordingIndex()
    # main.py + README.md each yield 1 chunk -> total 2, over a ceiling of 1.
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=1)
    code = _run("acme/widgets", idx, cfg=cfg, embed_fn=lambda texts: [[0.0] for _ in texts])
    assert code == 0  # the repo is NOT failed by a semantic-only problem
    assert idx.calls == ["acme/widgets"]  # core index still ran
    assert idx.chunk_writer is None  # ...with chunks skipped
    # Proves the core index got the real work, not an empty items generator: "not skipped"
    # and "correctly indexed" are different claims, and only the latter is the contract.
    assert idx.counts[0] == IndexCounts(files=2, symbols=1, swept=0)


@pytest.mark.unit
def test_embedder_failure_degrades_but_still_indexes_the_core() -> None:
    """Same contract for a downed embedder, which is the likelier production failure."""

    def _down(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("serving endpoint unavailable")

    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=True)
    code = _run("acme/widgets", idx, cfg=cfg, embed_fn=_down)
    assert code == 0
    assert idx.calls == ["acme/widgets"]
    assert idx.chunk_writer is None


@pytest.mark.unit
def test_unbuildable_embedder_does_not_abort_the_whole_run() -> None:
    """semantic_enabled with no configured endpoint must not kill every repo.

    get_embedder raises when semantic_embedding_endpoint is unset; letting that
    propagate out of run() would abort indexing for repos unrelated to semantic.
    """
    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=True, semantic_embedding_endpoint=None)
    code = _run("acme/widgets", idx, cfg=cfg)  # no embed_fn injected -> real get_embedder
    assert code == 0
    assert idx.calls == ["acme/widgets"]
    assert idx.chunk_writer is None


# --- main() exit semantics --------------------------------------------------
# A serverless python_wheel_task entry point must RETURN on success; any raised
# SystemExit (even code 0) is reported as a workload failure. main() must only
# exit non-zero, and only when a repo actually failed. (Regression: a live run
# failed with "SystemExit: 0" despite indexing succeeding.)


@pytest.mark.unit
def test_main_returns_normally_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import indexer.job as job

    monkeypatch.setattr(job.sys, "argv", ["code-search-index", "--scope", "s", "--key", "k"])
    monkeypatch.setattr(job, "run", lambda **_: 0)
    # Must NOT raise SystemExit on the success path.
    assert job.main() is None


@pytest.mark.unit
def test_main_exits_nonzero_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import indexer.job as job

    monkeypatch.setattr(job.sys, "argv", ["code-search-index", "--scope", "s", "--key", "k"])
    monkeypatch.setattr(job, "run", lambda **_: 1)
    with pytest.raises(SystemExit) as excinfo:
        job.main()
    assert excinfo.value.code == 1
