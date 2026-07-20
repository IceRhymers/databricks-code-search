"""Unit tests for indexer.job: read_github_token and orchestration.

Orchestration runs with every I/O boundary faked: a fake WorkspaceClient (secret
read), an injected ``config_loader`` (the workspace config read), an
httpx.MockTransport client (GitHub HTTP), a fake Engine/Connection, and a
recording ``index_fn`` — so no Databricks creds and no Postgres are needed.

``resolve_repos`` is NOT faked. Resolution runs for real against the mock
transport, so the fail-fast criteria exercise the actual wiring.

``normalize_repo``'s own tables live in ``tests/unit/test_repo_config.py`` after
the Decision 0 move; they are not duplicated here.
"""

from __future__ import annotations

import base64
import inspect
import io
import logging
import subprocess
import sys
import tarfile
from typing import Any

import httpx
import pytest

from app.config import Settings
from indexer.job import read_github_token, run
from indexer.languages import IndexCounts, ParsedFile
from indexer.repo_config import ConfigError, RepoConfig, load_config

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


class _GitHub:
    """A recording GitHub fake covering both enumeration and the index pipeline.

    ``enumerations`` maps a selector name (``orgs``/``users`` entry) to the repo
    objects that endpoint returns; an unlisted selector 404s, which is how the
    fail-fast criteria drive a failed enumeration. ``missing`` names repos whose
    ``resolve_ref`` metadata call 404s, failing them at *index* time only.
    """

    def __init__(
        self,
        *,
        enumerations: dict[str, list[dict[str, Any]]] | None = None,
        missing: set[str] | None = None,
    ) -> None:
        self.enumerations = enumerations or {}
        self.missing = missing or set()
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        parts = path.strip("/").split("/")

        # /orgs/{x}/repos and /users/{x}/repos — enumeration. Checked FIRST: an
        # /orgs/acme/repos path also has three segments and would otherwise fall
        # into the repo-metadata arm below.
        if len(parts) == 3 and parts[0] in {"orgs", "users"} and parts[2] == "repos":
            listing = self.enumerations.get(parts[1])
            if listing is None:
                return httpx.Response(404)
            return httpx.Response(200, json=listing)

        # /repos/{org}/{repo}...
        if len(parts) >= 3 and parts[0] == "repos":
            org, repo = parts[1], parts[2]
            if len(parts) == 3:
                if f"{org}/{repo}" in self.missing:
                    return httpx.Response(404)
                return httpx.Response(200, json={"default_branch": "main"})
            if parts[3] == "commits":
                return httpx.Response(200, json={"sha": f"sha_{repo}"})
            if parts[3] == "tarball":
                return httpx.Response(200, content=_tarball(f"{org}-{repo}-shashas"))
        return httpx.Response(404)

    @property
    def tarball_requests(self) -> list[str]:
        return [p for p in self.paths if "/tarball" in p]


def _repo_meta(full_name: str, **overrides: Any) -> dict[str, Any]:
    """One GitHub list-repos object, defaulting to a repo nothing excludes."""
    return {"full_name": full_name, "fork": False, "archived": False, "size": 10, **overrides}


def _config(**connection: Any) -> RepoConfig:
    """A single-github-connection RepoConfig from selector kwargs."""
    return RepoConfig.model_validate(
        {"version": 1, "connections": [{"type": "github", **connection}]}
    )


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


def _run(
    config: RepoConfig,
    index_fn: Any,
    *,
    cfg: Settings | None = None,
    embed_fn: Any = None,
    github: _GitHub | None = None,
) -> int:
    """Drive run() with a faked config read but a REAL resolve_repos."""
    wc = _FakeWorkspaceClient("tok")
    engine = _FakeEngine()
    handler = github if github is not None else _GitHub()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return run(
            config_path="/Workspace/x/config.yaml",
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
            config_loader=lambda _client, _path: config,
        )


@pytest.mark.unit
def test_run_indexes_all_repos() -> None:
    idx = _RecordingIndex()
    code = _run(_config(repos=["acme/widgets", "acme/gadgets"]), idx)
    assert code == 0
    assert set(idx.calls) == {"acme/widgets", "acme/gadgets"}


@pytest.mark.unit
def test_run_indexes_enumerated_repos() -> None:
    """The enumerated path reaches indexing, not just the explicit one."""
    idx = _RecordingIndex()
    github = _GitHub(enumerations={"acme": [_repo_meta("acme/widgets")]})
    code = _run(_config(orgs=["acme"]), idx, github=github)
    assert code == 0
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_run_parses_files_and_symbols() -> None:
    idx = _RecordingIndex()
    code = _run(_config(repos=["acme/widgets"]), idx)
    assert code == 0
    # main.py + README.md both stored; main.py yields one function symbol.
    assert idx.calls == ["acme/widgets"]
    assert idx.counts == [IndexCounts(files=2, symbols=1, swept=0)]


# --- AC 33: import health (the Decision 0 circular-import regression guard) ---
# Deliberately a SUBPROCESS check. This module has already imported indexer.job
# by the time any test body runs, so an in-process `import indexer.job` would be
# vacuous. Both directions are checked because a cycle fails in either order.


@pytest.mark.unit
@pytest.mark.parametrize(
    "statement",
    ["from indexer.job import main", "import indexer.resolve"],
)
def test_modules_import_in_a_cold_interpreter(statement: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# --- AC 34 ------------------------------------------------------------------
# Asserted here rather than in test_repo_config.py so the import-light model
# module's test file never imports indexer.job (Axis B1).


@pytest.mark.unit
def test_run_config_loader_defaults_to_load_config() -> None:
    assert inspect.signature(run).parameters["config_loader"].default is load_config


# --- AC 35 / 36 / 38 / 39 / 40: fail-fast before any indexing ---------------


@pytest.mark.unit
def test_run_fails_fast_when_an_enumeration_fails() -> None:
    """AC 35: the second org 404s -> nothing is indexed and no tarball is fetched."""
    idx = _RecordingIndex()
    github = _GitHub(enumerations={"acme": [_repo_meta("acme/widgets")]})  # "other" 404s
    code = _run(_config(orgs=["acme", "other"]), idx, github=github)
    assert code == 1
    assert idx.calls == []
    assert github.tarball_requests == []


@pytest.mark.unit
def test_run_returns_1_when_config_resolves_to_zero_repos() -> None:
    """AC 36: an org that enumerates only excluded repos resolves to nothing."""
    idx = _RecordingIndex()
    github = _GitHub(enumerations={"acme": [_repo_meta("acme/f", fork=True)]})
    code = _run(_config(orgs=["acme"]), idx, github=github)
    assert code == 1
    assert idx.calls == []
    assert github.tarball_requests == []


@pytest.mark.unit
def test_run_isolates_failing_repo() -> None:
    """AC 37: both repos resolve; the second's resolve_ref 404s at INDEX time.

    Per-repo isolation is a property of the indexing loop, so it can only be
    exercised by a failure that occurs *after* resolution succeeds. A malformed
    entry now fails at resolution instead — see AC 38.
    """
    idx = _RecordingIndex()
    github = _GitHub(missing={"acme/gadgets"})
    code = _run(_config(repos=["acme/widgets", "acme/gadgets"]), idx, github=github)
    assert code == 1
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_malformed_explicit_repo_aborts_the_whole_run() -> None:
    """AC 38: a documented semantic change.

    A bad explicit entry used to be isolated to its own repo, with the others
    still indexed. It now raises out of resolve_repos, so the run indexes
    nothing at all. Asserted so the change is a tested contract, not a side
    effect.
    """
    idx = _RecordingIndex()
    github = _GitHub()
    code = _run(_config(repos=["acme/widgets", "https://evil.com/a/b"]), idx, github=github)
    assert code == 1
    assert idx.calls == []
    assert github.tarball_requests == []


@pytest.mark.unit
def test_bare_value_error_from_resolution_returns_1(caplog: pytest.LogCaptureFixture) -> None:
    """AC 40: normalize_repo's bare ValueError must not escape as a traceback.

    The handler catches `Exception`, not a named tuple — a narrow catch would
    let this one through and break main()'s exit-code contract.

    Distinct from test_malformed_explicit_repo_aborts_the_whole_run above, which
    pins the *semantic* change (nothing gets indexed). This one pins the
    *diagnostic* contract: exit 1 is reached via a logged, traceback-carrying
    resolve-phase record, not by the exception escaping to the interpreter.
    """
    idx = _RecordingIndex()
    with caplog.at_level(logging.ERROR, logger="indexer.job"):
        code = _run(_config(repos=["https://evil.com/a/b"]), idx)
    assert code == 1
    assert idx.calls == []

    errors = [r for r in caplog.records if r.name == "indexer.job"]
    assert len(errors) == 1
    # Names the resolve phase, so it is distinguishable from the config-load
    # failure point (which now also catches Exception).
    assert "could not resolve repos" in errors[0].getMessage()
    assert errors[0].exc_info is not None


@pytest.mark.unit
def test_config_error_returns_1_without_opening_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 39: a failed config read never reaches create_db_engine.

    ``engine=None`` is required: the injected-engine path never calls
    create_db_engine at all, which would make the assertion vacuous.
    """
    import indexer.job as job

    calls: list[Any] = []
    monkeypatch.setattr(job, "create_db_engine", lambda **kw: calls.append(kw))

    def _boom(_client: Any, _path: str) -> RepoConfig:
        raise ConfigError("failed to read config from '/Workspace/x/config.yaml' (HTTP 404)")

    idx = _RecordingIndex()
    with httpx.Client(transport=httpx.MockTransport(_GitHub())) as client:
        code = run(
            config_path="/Workspace/x/config.yaml",
            scope="s",
            key="k",
            endpoint="ep",
            database="db",
            workspace_client=_FakeWorkspaceClient("tok"),
            http_client=client,
            engine=None,
            index_fn=idx,
            config_loader=_boom,
        )
    assert code == 1
    assert calls == []
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
    code = _run(_config(repos=["acme/widgets"]), idx, cfg=Settings(semantic_enabled=False))
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
    code = _run(_config(repos=["acme/widgets"]), idx, cfg=cfg, embed_fn=fake_embed)
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
    code = _run(
        _config(repos=["acme/widgets"]), idx, cfg=cfg, embed_fn=lambda texts: [[0.0] for _ in texts]
    )
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
    code = _run(_config(repos=["acme/widgets"]), idx, cfg=cfg, embed_fn=_down)
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
    # No embed_fn injected -> the real get_embedder runs.
    code = _run(_config(repos=["acme/widgets"]), idx, cfg=cfg)
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

    argv = ["code-search-index", "--config", "/Workspace/x/config.yaml"]
    monkeypatch.setattr(job.sys, "argv", [*argv, "--scope", "s", "--key", "k"])
    monkeypatch.setattr(job, "run", lambda **_: 0)
    # Must NOT raise SystemExit on the success path.
    assert job.main() is None


@pytest.mark.unit
def test_main_accepts_the_underscored_max_repos_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """resources/job.yml spells the key ``max_repos``, and python_wheel_task emits
    it verbatim as ``--max_repos=<value>``. A hyphenated flag would fail only on a
    live run, invisibly to every other gate here."""
    import indexer.job as job

    seen: dict[str, Any] = {}
    argv = ["code-search-index", "--config", "/c.yaml", "--max_repos=7"]
    monkeypatch.setattr(job.sys, "argv", [*argv, "--scope", "s", "--key", "k"])
    monkeypatch.setattr(job, "run", lambda **kw: seen.update(kw) or 0)
    job.main()
    assert seen["max_repos"] == 7
    assert seen["config_path"] == "/c.yaml"


@pytest.mark.unit
def test_main_rejects_a_max_repos_below_one(monkeypatch: pytest.MonkeyPatch) -> None:
    import indexer.job as job

    argv = ["code-search-index", "--config", "/c.yaml", "--max_repos=0"]
    monkeypatch.setattr(job.sys, "argv", [*argv, "--scope", "s", "--key", "k"])
    with pytest.raises(SystemExit):
        job.main()


@pytest.mark.unit
def test_main_exits_nonzero_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import indexer.job as job

    argv = ["code-search-index", "--config", "/Workspace/x/config.yaml"]
    monkeypatch.setattr(job.sys, "argv", [*argv, "--scope", "s", "--key", "k"])
    monkeypatch.setattr(job, "run", lambda **_: 1)
    with pytest.raises(SystemExit) as excinfo:
        job.main()
    assert excinfo.value.code == 1
