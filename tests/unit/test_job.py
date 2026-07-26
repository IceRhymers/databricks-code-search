"""Unit tests for indexer.job: read_github_token and orchestration.

Orchestration runs with every I/O boundary faked: a fake WorkspaceClient (secret
read), an injected ``config_loader`` (the workspace config read), an
httpx.MockTransport client (GitHub HTTP), a fake Engine/Connection, and a
recording ``index_fn`` — so no Databricks creds and no Postgres are needed.

``resolve_repos`` is NOT faked. Resolution runs for real against the mock
transport, so the fail-fast criteria exercise the actual wiring.

``normalize_repo``'s own tables live in ``tests/unit/test_repo_config.py`` after
that move; they are not duplicated here.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import io
import logging
import re
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest

from app.config import Settings
from app.db.models import INDEX_SEMANTICS_VERSION
from indexer.extract_pool import ExtractionPool, ExtractionPoolError
from indexer.hashing import content_sha
from indexer.job import (
    BranchOutcome,
    RepoOutcome,
    _decide_reconciliation,
    _reconcile,
    _reconciliation_skip_reason,
    read_github_token,
    run,
)
from indexer.languages import ExtractedSymbol, FileExtraction, IndexCounts, ParsedFile
from indexer.repo_config import ConfigError, RepoConfig, load_config
from indexer.resolve import RepoEntry
from indexer.store import (
    ReconcileCounts,
    StaleIndexError,
    reconcile_removed_repos,
    reconcile_retired_branches,
)

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


_DEFAULT_FILES = {
    "main.py": b"def f():\n    return 1\n",
    "README.md": b"# hi\n",
}


def _tarball(top_dir: str, files: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, data in (files or _DEFAULT_FILES).items():
            name = f"{top_dir}/{rel}"
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
        files: dict[str, bytes] | None = None,
        branches: dict[str, list[str]] | None = None,
        branches_fail: set[str] | None = None,
        tarball_bytes: bytes | None = None,
    ) -> None:
        self.enumerations = enumerations or {}
        self.missing = missing or set()
        self.files = files
        # Verbatim archive bytes for the /tarball endpoint, bypassing `_tarball`.
        # The only way to serve a MALFORMED archive, which is what proves where
        # streaming validation now happens (D8).
        self.tarball_bytes = tarball_bytes
        # full_name -> branch names, for the /branches endpoint. A repo not
        # listed here answers with an empty branch list.
        self.branches = branches or {}
        # full_name entries here answer the /branches endpoint with a 500,
        # exercising the complete-or-raise contract at the job level.
        self.branches_fail = branches_fail or set()
        # Appended to from worker threads under fan-out; list.append is atomic,
        # so the contents are safe even though the ORDER is not deterministic.
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
                # "main" keeps the pre-multi-branch sha_{repo} shape every
                # existing test relies on; any other ref gets a distinguishable
                # sha so per-branch tests can tell which ref was resolved.
                ref = parts[4] if len(parts) > 4 else "main"
                sha = f"sha_{repo}" if ref == "main" else f"sha_{repo}_{ref}"
                return httpx.Response(200, json={"sha": sha})
            if parts[3] == "branches":
                if f"{org}/{repo}" in self.branches_fail:
                    return httpx.Response(500)
                names = self.branches.get(f"{org}/{repo}", [])
                return httpx.Response(200, json=[{"name": n} for n in names])
            if parts[3] == "tarball":
                if self.tarball_bytes is not None:
                    return httpx.Response(200, content=self.tarball_bytes)
                return httpx.Response(200, content=_tarball(f"{org}-{repo}-shashas", self.files))
        return httpx.Response(404)

    @property
    def tarball_requests(self) -> list[str]:
        return [p for p in self.paths if "/tarball" in p]


def _repo_meta(full_name: str, **overrides: Any) -> dict[str, Any]:
    """One GitHub list-repos object, defaulting to a repo nothing excludes."""
    return {"full_name": full_name, "fork": False, "archived": False, "size": 10, **overrides}


def _config(
    *,
    index_concurrency: int | None = None,
    extract_processes: int | None = 1,
    semantic_max_chunks_per_repo: dict[str, int] | None = None,
    semantic: dict[str, Any] | None = None,
    **connection: Any,
) -> RepoConfig:
    """A single-github-connection RepoConfig from selector kwargs.

    ``semantic=`` sets the top-level ``semantic:`` overlay block (config.yaml's
    job-side semantic-config surface), mirroring the ``semantic_max_chunks_per_repo``
    per-repo-map kwarg above -- the two are distinct config surfaces (global INT vs
    per-repo MAP) and either can be set independently.

    ``extract_processes`` (#108) defaults to ``1`` -- the pool KILL SWITCH --
    deliberately, not to the schema's own ``None`` default. Left at ``None``, every
    one of the ~150 ``run()`` tests below would run the preflight probe and spawn
    real worker processes on every call, each re-importing
    ``tree_sitter_language_pack``: minutes slower and flaky on a constrained CI
    runner. Only the pool-specific tests in ``tests/unit/test_extract_pool.py``
    (and the dedicated pool-lifecycle tests here) opt into a real pool by passing
    ``extract_processes=`` explicitly.
    """
    doc: dict[str, Any] = {"version": 1, "connections": [{"type": "github", **connection}]}
    if index_concurrency is not None:
        doc["index_concurrency"] = index_concurrency
    if extract_processes is not None:
        doc["extract_processes"] = extract_processes
    if semantic_max_chunks_per_repo is not None:
        doc["semantic_max_chunks_per_repo"] = semantic_max_chunks_per_repo
    if semantic is not None:
        doc["semantic"] = semantic
    return RepoConfig.model_validate(doc)


class _StampRow(NamedTuple):
    """One row of the pre-fan-out stamp SELECT (repo_branches JOIN repos)."""

    name: str
    branch: str
    last_indexed_commit: str | None
    index_semantics_version: int | None


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _FakeResult:
        """Answers the reconciliation checkpoint's ``select(Repo.name)`` (stored repos)."""
        return self


class _FakeConn:
    def __init__(self, engine: _FakeEngine | None = None) -> None:
        self._engine = engine

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def rollback(self) -> None:
        """No-op: real code clears the purge-guard read's autobegun txn here."""

    def execute(self, stmt: Any) -> _FakeResult:
        assert self._engine is not None
        self._engine.executed.append(stmt)
        # Routed by statement shape, not identity: the pre-fan-out stamp SELECT
        # joins repo_branches (see _read_stamps); the reconciliation checkpoint's
        # stored-repo-names SELECT (job.py's _reconcile) does not. Real
        # reconcile_retired_fn/reconcile_removed_fn primitives issue raw text()
        # DML and are never exercised here -- _run() default-injects no-op fakes
        # for both (see _noop_retired_fn/_noop_removed_fn) precisely so this fake
        # conn never has to answer them.
        if "repo_branches" in str(stmt):
            return _FakeResult(self._engine.stamp_rows)
        return _FakeResult(self._engine.repo_names)


class _FakeEngine:
    """Fake Engine that also answers ``_read_stamps``' and ``_reconcile``'s batched SELECTs.

    ``stamps`` maps a ``(repo name, branch)`` pair to its stored
    ``(last_indexed_commit, index_semantics_version)``; an absent pair is simply
    not returned, which is the "never indexed" shape. Every test repo here
    resolves to just its "main" branch (no ``branches:`` configured), so a
    stamp is keyed ``(name, "main")``.

    ``repo_names`` answers the reconciliation checkpoint's unfiltered
    ``select(Repo.name)`` (the purge shrink guard's "what's currently stored"
    read) -- defaults to empty, which keeps the guard a no-op (nothing stored ->
    nothing can be a majority of it) for every test that does not care about it.
    """

    def __init__(
        self,
        stamps: dict[tuple[str, str], tuple[str | None, int | None]] | None = None,
        repo_names: list[str] | None = None,
    ) -> None:
        self.disposed = False
        self.disposed_at: float | None = None
        self.executed: list[Any] = []
        self.stamp_rows = [
            _StampRow(name, branch, commit, version)
            for (name, branch), (commit, version) in (stamps or {}).items()
        ]
        self.repo_names = repo_names or []

    def connect(self) -> _FakeConn:
        return _FakeConn(self)

    def dispose(self) -> None:
        self.disposed = True
        self.disposed_at = time.monotonic()


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
        branch: str,
        is_default: bool,
        head_sha: str,
        items: Any,
        chunk_writer: Any = None,
    ) -> IndexCounts:
        materialized = list(items)
        self.calls.append(name)
        self.chunk_writer = chunk_writer
        files = len(materialized)
        symbols = sum(len(ex.symbols) for _pf, ex in materialized)
        edges = sum(len(ex.edges) for _pf, ex in materialized)
        counts = IndexCounts(files=files, symbols=symbols, swept=0, edges=edges)
        self.counts.append(counts)
        return counts


def _noop_retired_fn(conn: Any, *, name: str, retired_branches: Any) -> ReconcileCounts:
    return ReconcileCounts(branches_removed=0, files_stripped=0, files_deleted=0)


def _noop_removed_fn(conn: Any, *, desired_repos: Any) -> list[str]:
    return []


def _noop_shas_fn(conn: Any, *, name: str, branch: str) -> tuple[set[Any], set[Any]]:
    """Default ``shas_fn`` for ``_run()`` -- ``_FakeConn`` cannot answer the real
    ``read_indexed_shas`` query (it routes only on ``"repo_branches" in str(stmt)``,
    per its own docstring), so any test that reaches the advisory read without an
    explicit override would call the REAL primitive against the fake engine and
    fail. Returning two empty sets degrades to "everything is changed/new",
    matching ``read_indexed_shas``' own behaviour for a never-indexed repo -- safe
    in the correct direction and inert for every test that never exercises delta
    embedding at all."""
    return set(), set()


class _RecordingReconcile:
    """Fake ``reconcile_retired_fn``/``reconcile_removed_fn`` pair, explicitly opted into.

    Records every call (sorted, so assertions don't depend on set/dict
    iteration order) instead of touching a real Postgres connection. Pass
    ``retired_result``/``removed_result`` to script a return value, or
    ``retired_raises``/``removed_raises`` to have that side raise instead --
    used by the failure-handling tests.
    """

    def __init__(
        self,
        *,
        retired_result: ReconcileCounts | None = None,
        removed_result: list[str] | None = None,
        retired_raises: Exception | None = None,
        removed_raises: Exception | None = None,
    ) -> None:
        self.retired_calls: list[tuple[str, list[str]]] = []
        self.removed_calls: list[list[str]] = []
        self._retired_result = retired_result or ReconcileCounts(0, 0, 0)
        self._removed_result = [] if removed_result is None else removed_result
        self._retired_raises = retired_raises
        self._removed_raises = removed_raises

    def retired_fn(self, conn: Any, *, name: str, retired_branches: Any) -> ReconcileCounts:
        self.retired_calls.append((name, sorted(retired_branches)))
        if self._retired_raises is not None:
            raise self._retired_raises
        return self._retired_result

    def removed_fn(self, conn: Any, *, desired_repos: Any) -> list[str]:
        self.removed_calls.append(sorted(desired_repos))
        if self._removed_raises is not None:
            raise self._removed_raises
        return self._removed_result


def _run(
    config: RepoConfig,
    index_fn: Any,
    *,
    cfg: Settings | None = None,
    embed_fn: Any = None,
    github: _GitHub | None = None,
    engine: _FakeEngine | None = None,
    reconcile_retired_fn: Any = _noop_retired_fn,
    reconcile_removed_fn: Any = _noop_removed_fn,
    shas_fn: Any = _noop_shas_fn,
    extraction_pool: Any = None,
) -> int:
    """Drive run() with a faked config read but a REAL resolve_repos.

    ``reconcile_retired_fn``/``reconcile_removed_fn`` default to no-ops: a clean
    run now always reaches the post-fan-out reconciliation checkpoint, and
    without an override here it would call the REAL ``indexer.store`` primitives
    against ``_FakeEngine``/``_FakeConn`` (neither implements ``conn.begin()``).
    Tests that assert on reconciliation itself pass an explicit
    :class:`_RecordingReconcile`'s bound methods.

    ``shas_fn`` defaults to :func:`_noop_shas_fn` for the same reason: a
    version-matching, sha-mismatching stamp with semantic indexing on would
    otherwise reach the REAL ``read_indexed_shas`` against ``_FakeConn``, which
    cannot answer it (see that fake's docstring).

    ``extraction_pool`` (#108) defaults to ``None``, in which case ``run()``
    builds its own from ``config.extract_processes`` (pinned to ``1``, the kill
    switch, by ``_config``'s own default -- see T9). Pass an explicit fake/real
    :class:`~indexer.extract_pool.ExtractionPool` to test the pool seam itself.
    """
    wc = _FakeWorkspaceClient("tok")
    engine = engine if engine is not None else _FakeEngine()
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
            reconcile_retired_fn=reconcile_retired_fn,
            reconcile_removed_fn=reconcile_removed_fn,
            shas_fn=shas_fn,
            extraction_pool=extraction_pool,
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
    assert idx.counts == [IndexCounts(files=2, symbols=1, swept=0, edges=0)]


# --- import health (the circular-import regression guard) -------------------
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


# --- config_loader defaults to load_config -----------------------------------
# Asserted here rather than in test_repo_config.py so the import-light model
# module's test file never imports indexer.job.


@pytest.mark.unit
def test_run_config_loader_defaults_to_load_config() -> None:
    assert inspect.signature(run).parameters["config_loader"].default is load_config


# --- fail-fast before any indexing -------------------------------------------


@pytest.mark.unit
def test_run_fails_fast_when_an_enumeration_fails() -> None:
    """The second org 404s -> nothing is indexed and no tarball is fetched."""
    idx = _RecordingIndex()
    github = _GitHub(enumerations={"acme": [_repo_meta("acme/widgets")]})  # "other" 404s
    code = _run(_config(orgs=["acme", "other"]), idx, github=github)
    assert code == 1
    assert idx.calls == []
    assert github.tarball_requests == []


@pytest.mark.unit
def test_run_returns_1_when_config_resolves_to_zero_repos() -> None:
    """An org that enumerates only excluded repos resolves to nothing."""
    idx = _RecordingIndex()
    github = _GitHub(enumerations={"acme": [_repo_meta("acme/f", fork=True)]})
    code = _run(_config(orgs=["acme"]), idx, github=github)
    assert code == 1
    assert idx.calls == []
    assert github.tarball_requests == []


@pytest.mark.unit
def test_run_isolates_failing_repo() -> None:
    """Both repos resolve; the second's resolve_ref 404s at INDEX time.

    Per-repo isolation is a property of the indexing loop, so it can only be
    exercised by a failure that occurs *after* resolution succeeds. A malformed
    entry now fails at resolution instead -- see the test below.
    """
    idx = _RecordingIndex()
    github = _GitHub(missing={"acme/gadgets"})
    code = _run(_config(repos=["acme/widgets", "acme/gadgets"]), idx, github=github)
    assert code == 1
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_malformed_explicit_repo_aborts_the_whole_run() -> None:
    """A documented semantic change.

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
    """normalize_repo's bare ValueError must not escape as a traceback.

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
    """A failed config read never reaches create_db_engine.

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


# --- semantic chunk_writer wiring --------------------------------------------
# Chunks are chunked+embedded OUTSIDE index_repo's transaction: run()/
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
    # lazily per file inside index_repo's transaction.
    assert len(embed_calls) == 1
    assert len(embed_calls[0]) == 2

    # Exercise the closure directly: writing main.py's chunks is a
    # delete-then-insert against the chunks table, keyed by the given file_id.
    # The closure now takes a batch -- a sequence of (file_id, pf) pairs.
    conn = _FakeChunkConn()
    pf = ParsedFile(path="main.py", lang="python", size=10, content="def f():\n    return 1\n")
    idx.chunk_writer(conn, 1, [(42, pf)])
    assert len(conn.calls) == 2
    delete_stmt, delete_params = conn.calls[0]
    assert delete_stmt.table.name == "chunks"
    assert delete_params == {"ids": [42]}
    insert_stmt, values = conn.calls[1]
    assert insert_stmt.table.name == "chunks"
    assert values is None  # rows travel inside stmt.values(...), not as a param list
    assert insert_stmt._multi_values[0] == [
        {
            "file_id": 42,
            "chunk_index": 0,
            "content": "def f():\n    return 1\n",
            "start_line": 1,
            "end_line": 2,
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
    assert idx.counts[0] == IndexCounts(files=2, symbols=1, swept=0, edges=0)


@pytest.mark.unit
def test_semantic_cap_override_lets_a_repo_exceed_the_global_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repo named in ``semantic_max_chunks_per_repo`` gets ITS cap, not the global one.

    Same fixture repo as the ceiling test above (main.py + README.md -> 2 chunks
    total), but here the global cap (1) would fail it while the per-repo override
    (5) does not -- proving the override, not just the global default, is what
    reaches ``_precompute_chunk_writer``.
    """
    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=1)
    config = _config(repos=["acme/widgets"], semantic_max_chunks_per_repo={"acme/widgets": 5})
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(config, idx, cfg=cfg, embed_fn=lambda texts: [[0.0] for _ in texts])
    assert code == 0
    assert idx.calls == ["acme/widgets"]
    assert idx.chunk_writer is not None  # the override, not the global cap of 1, applied
    messages = [r.getMessage() for r in caplog.records if r.name == "indexer.job"]
    assert "semantic chunk cap override for acme/widgets: 5 (global 1)" in messages


@pytest.mark.unit
def test_semantic_cap_override_exceeded_still_degrades_to_core_index() -> None:
    """Blowing through an OVERRIDE cap keeps the additive-degrade contract.

    The override changes the number, never the failure mode: a repo over its
    per-repo cap must still get its core index (files/symbols/sweep) with only
    the chunks skipped, exactly like the global-cap test above.
    """
    idx = _RecordingIndex()
    # Global cap of 5 would pass the 2-chunk fixture; the override of 1 fails it.
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=5)
    config = _config(repos=["acme/widgets"], semantic_max_chunks_per_repo={"acme/widgets": 1})
    code = _run(config, idx, cfg=cfg, embed_fn=lambda texts: [[0.0] for _ in texts])
    assert code == 0  # a semantic-only breach never fails the repo
    assert idx.calls == ["acme/widgets"]
    assert idx.chunk_writer is None  # the override (1), not the global cap (5), fired
    assert idx.counts[0] == IndexCounts(files=2, symbols=1, swept=0, edges=0)


@pytest.mark.unit
def test_semantic_cap_without_a_matching_override_still_enforces_the_global_cap() -> None:
    """An override for a DIFFERENT repo must not leak into this repo's effective cap."""
    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=1)
    config = _config(repos=["acme/widgets"], semantic_max_chunks_per_repo={"acme/other": 5})
    code = _run(config, idx, cfg=cfg, embed_fn=lambda texts: [[0.0] for _ in texts])
    assert code == 0
    assert idx.calls == ["acme/widgets"]
    assert idx.chunk_writer is None  # no override matched -- the global cap of 1 still fires


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


# --- file-level delta indexing: the chunk_writer covered-set guard (#104) ---


@pytest.mark.unit
def test_chunk_writer_covered_guard_skips_an_uncovered_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T14: a path outside _precompute_chunk_writer's own file list -> WARNING, and
    chunk_writer issues NO statement at all (specifically no DELETE FROM chunks,
    which write_chunks always opens with -- see indexer/chunk_store.py). This is
    the defence-in-depth guard for a path the single-writer-per-repo invariant
    says index_repo can never actually pass it; unreachable in production, but
    the alternative (silently deleting an uncovered file's chunk rows) is worse
    than a loud skip.
    """
    from indexer.job import _precompute_chunk_writer

    pf = ParsedFile(path="ghost.py", lang="python", size=10, content="x = 1\n")
    chunk_writer = _precompute_chunk_writer([], lambda texts: [[0.0] for _ in texts], 100)
    conn = _FakeChunkConn()
    with caplog.at_level(logging.WARNING, logger="indexer.job"):
        chunk_writer(conn, 1, [(99, pf)])
    assert conn.calls == []
    assert any(
        "no precomputed chunks for ghost.py" in r.getMessage()
        for r in caplog.records
        if r.name == "indexer.job"
    )


@pytest.mark.unit
def test_chunk_writer_covers_every_embedded_path_including_zero_chunk_files() -> None:
    """The covered set is `set(per_file)`, not `set(by_path)` -- a file that embeds
    to zero chunks (e.g. an empty file) is still COVERED, so its chunk_writer call
    reaches write_chunks([]) (the delete-only, zero-row-insert shape) rather than
    the warn-and-skip guard above."""
    from indexer.job import _precompute_chunk_writer

    empty_pf = ParsedFile(path="empty.py", lang="python", size=0, content="")
    chunk_writer = _precompute_chunk_writer([empty_pf], lambda texts: [[0.0] for _ in texts], 100)
    conn = _FakeChunkConn()
    chunk_writer(conn, 1, [(99, empty_pf)])
    # write_chunks_batch always issues its DELETE even for zero chunks (see
    # indexer/chunk_store.py) -- so a real (non-warning) statement was issued.
    assert len(conn.calls) >= 1


# --- file-level delta indexing: the advisory shas_fn seam (#104) ------------
# job.py's copy of indexer.store.read_repo_content_shas is ADVISORY -- it only
# narrows what _precompute_chunk_writer embeds. index_repo's own read (behind
# index_fn, unexercised by _RecordingIndex) is the authoritative classification;
# these tests pin job.py's half of the contract only: when shas_fn is called,
# with what, and what that narrows the embedder's input to. _DEFAULT_FILES is
# {"main.py": b"def f():\n    return 1\n", "README.md": b"# hi\n"} (both text,
# both chunked -- indexer.parse.iter_chunks has no language gate).

_MAIN_SHA = content_sha("def f():\n    return 1\n")
_README_SHA = content_sha("# hi\n")


class _RecordingShas:
    """Fake ``shas_fn``: records every ``(name, branch)`` call and returns a
    scripted ``(carried, present)`` pair (``present`` is unused by job.py, which
    only classifies "unchanged" from ``carried`` -- the provenance gate that
    would also need ``present`` is index_repo's alone)."""

    def __init__(
        self,
        *,
        carried: set[tuple[str, str]] | None = None,
        present: set[tuple[str, str]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self._carried = carried or set()
        self._present = present or set()

    def __call__(self, conn: Any, *, name: str, branch: str) -> tuple[set[Any], set[Any]]:
        self.calls.append((name, branch))
        return set(self._carried), set(self._present)


@pytest.mark.unit
def test_shas_fn_called_once_before_embedding_when_version_matches_and_sha_differs() -> None:
    """T9: the gate is (stored version == current) AND (stored sha != HEAD) -- the
    latter is implied by reaching this code at all (an exact stamp match skips the
    branch entirely before any of this runs, see the Step 4 tests above)."""
    order: list[str] = []
    shas = _RecordingShas()

    def _shas_fn(conn: Any, *, name: str, branch: str) -> tuple[set[Any], set[Any]]:
        order.append("shas")
        return shas(conn, name=name, branch=branch)

    def _embed(texts: list[str]) -> list[list[float]]:
        order.append("embed")
        return [[0.0] for _ in texts]

    engine = _FakeEngine(stamps={("acme/widgets", "main"): ("sha_old", INDEX_SEMANTICS_VERSION)})
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    code = _run(
        _config(repos=["acme/widgets"]),
        _RecordingIndex(),
        cfg=cfg,
        embed_fn=_embed,
        engine=engine,
        shas_fn=_shas_fn,
    )
    assert code == 0
    assert shas.calls == [("acme/widgets", "main")]
    assert order == ["shas", "embed"]


@pytest.mark.unit
def test_shas_fn_not_called_when_the_semantics_version_is_stale() -> None:
    """T10: version mismatch -> the delta gate is closed, so job.py never issues the
    advisory read either, and the embedder receives every file's chunk text."""
    shas = _RecordingShas()
    embed_calls: list[list[str]] = []

    def _embed(texts: list[str]) -> list[list[float]]:
        embed_calls.append(list(texts))
        return [[0.0] for _ in texts]

    engine = _FakeEngine(
        stamps={("acme/widgets", "main"): ("sha_widgets", INDEX_SEMANTICS_VERSION - 1)}
    )
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    code = _run(
        _config(repos=["acme/widgets"]),
        _RecordingIndex(),
        cfg=cfg,
        embed_fn=_embed,
        engine=engine,
        shas_fn=shas,
    )
    assert code == 0
    assert shas.calls == []
    assert len(embed_calls) == 1
    # Both main.py's and README.md's chunk text are present -- nothing narrowed.
    joined = "\n".join(embed_calls[0])
    assert "def f" in joined
    assert "# hi" in joined


@pytest.mark.unit
def test_shas_fn_not_called_for_a_never_indexed_repo() -> None:
    """T10b: a missing stamp degrades to (None, None) -- version is None, never equal
    to INDEX_SEMANTICS_VERSION, so this is the same gate-closed path as a stale
    version, exercised separately because it is the far more common real case (a
    repo's first index) than an explicit version regression."""
    shas = _RecordingShas()
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    code = _run(
        _config(repos=["acme/widgets"]),
        _RecordingIndex(),
        cfg=cfg,
        embed_fn=lambda texts: [[0.0] for _ in texts],
        shas_fn=shas,
    )
    assert code == 0
    assert shas.calls == []


@pytest.mark.unit
def test_shas_fn_narrows_the_embed_set_to_not_unchanged_files() -> None:
    """T11: README.md is reported unchanged (carried); main.py is not -- the embedder
    must receive only main.py's chunk text."""
    embed_calls: list[list[str]] = []

    def _embed(texts: list[str]) -> list[list[float]]:
        embed_calls.append(list(texts))
        return [[0.0] for _ in texts]

    shas_fn = _RecordingShas(carried={("README.md", _README_SHA)})
    engine = _FakeEngine(stamps={("acme/widgets", "main"): ("sha_old", INDEX_SEMANTICS_VERSION)})
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    code = _run(
        _config(repos=["acme/widgets"]),
        _RecordingIndex(),
        cfg=cfg,
        embed_fn=_embed,
        engine=engine,
        shas_fn=shas_fn,
    )
    assert code == 0
    assert len(embed_calls) == 1
    joined = "\n".join(embed_calls[0])
    assert "def f" in joined
    assert "# hi" not in joined


@pytest.mark.unit
def test_shas_fn_all_unchanged_calls_the_embedder_with_zero_texts() -> None:
    """T12 (issue AC1): every file carried -> the embed list is empty, and
    _precompute_chunk_writer's own `all_texts` guard means the embedder is not
    even called (matching read_indexed_shas' safe-degrade convention: an unused
    injection point is never exercised, not called with an empty list)."""
    embed_calls: list[list[str]] = []

    def _embed(texts: list[str]) -> list[list[float]]:
        embed_calls.append(list(texts))
        return [[0.0] for _ in texts]

    shas_fn = _RecordingShas(
        carried={("README.md", _README_SHA), ("main.py", _MAIN_SHA)},
    )
    engine = _FakeEngine(stamps={("acme/widgets", "main"): ("sha_old", INDEX_SEMANTICS_VERSION)})
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    idx = _RecordingIndex()
    code = _run(
        _config(repos=["acme/widgets"]),
        idx,
        cfg=cfg,
        embed_fn=_embed,
        engine=engine,
        shas_fn=shas_fn,
    )
    assert code == 0
    assert embed_calls == []
    # The core index still ran over BOTH files -- narrowing the embed set never
    # narrows what index_fn (the store) sees; that is index_repo's classification
    # to make, from its own authoritative read.
    assert idx.counts == [IndexCounts(files=2, symbols=1, swept=0, edges=0)]
    assert idx.chunk_writer is not None


@pytest.mark.unit
def test_indexed_summary_line_is_byte_identical_regardless_of_delta_narrowing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T13: the drain loop's `indexed name@branch: files=.. symbols=.. edges=.. swept=..`
    line is IndexCounts' own format -- narrowing the embed set must not touch it."""
    shas_fn = _RecordingShas(carried={("README.md", _README_SHA)})
    engine = _FakeEngine(stamps={("acme/widgets", "main"): ("sha_old", INDEX_SEMANTICS_VERSION)})
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            _RecordingIndex(),
            cfg=cfg,
            embed_fn=lambda texts: [[0.0] for _ in texts],
            engine=engine,
            shas_fn=shas_fn,
        )
    assert code == 0
    indexed_lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "indexer.job" and r.getMessage().startswith("indexed ")
    ]
    assert indexed_lines == ["indexed acme/widgets@main: files=2 symbols=1 edges=0 swept=0"]


# --- file-level delta indexing: degraded-semantics run summary (#104) -------


@pytest.mark.unit
def test_precompute_failure_marks_the_branch_outcome_degraded() -> None:
    """A chunk-precompute failure marks the resulting BranchOutcome, not just the
    per-branch WARNING already covered by
    test_embedder_failure_degrades_but_still_indexes_the_core."""
    from indexer.job import _index_one_branch

    def _down(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("serving endpoint unavailable")

    with httpx.Client(transport=httpx.MockTransport(_GitHub())) as client:
        outcome = _index_one_branch(
            "acme/widgets",
            org="acme",
            repo="widgets",
            branch="main",
            is_default=True,
            default_head_sha="sha_widgets",
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_RecordingIndex(),
            cfg=Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100),
            embed_fn=_down,
            stamps={},
            started=time.monotonic(),
            max_chunks_per_repo=100,
            shas_fn=_noop_shas_fn,
            extraction_pool=ExtractionPool(n_processes=0),
        )
    assert outcome.status == "indexed"
    assert outcome.semantic_degraded is True


@pytest.mark.unit
def test_run_emits_one_aggregate_warning_naming_every_degraded_branch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The run-completion WARNING is the greppable record the runbook remedy
    (clear the branch's semantics stamp) depends on -- the per-branch warning at
    the precompute site is easy to miss in a large run's log."""

    def _down(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("serving endpoint unavailable")

    cfg = Settings(semantic_enabled=True)
    with caplog.at_level(logging.WARNING, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), _RecordingIndex(), cfg=cfg, embed_fn=_down)
    assert code == 0
    warnings = [r.getMessage() for r in caplog.records if r.name == "indexer.job"]
    aggregate = [m for m in warnings if m.startswith("1 branch(es) finished with degraded")]
    assert len(aggregate) == 1
    assert "acme/widgets@main" in aggregate[0]


@pytest.mark.unit
def test_run_emits_no_aggregate_warning_when_nothing_degraded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), _RecordingIndex())
    assert code == 0
    warnings = [r.getMessage() for r in caplog.records if r.name == "indexer.job"]
    assert not any("degraded semantic coverage" in m for m in warnings)


# --- config.yaml `semantic:` overlay onto cfg (config.yaml > env > default) --


@pytest.mark.unit
def test_config_yaml_semantic_disabled_beats_env_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``semantic.enabled: false`` in config.yaml wins over an env-enabled cfg.

    The job has no reachable env surface, so config.yaml is authoritative. With the
    overlay applied before the embedder build, an injected embed_fn is never called
    and no chunk_writer is wired -- a true semantic no-op even though ``cfg`` says
    enabled. The applied-overrides line names the field (not its value).
    """

    def _poison(_texts: list[str]) -> list[list[float]]:
        raise AssertionError("embedder must not run when config.yaml disabled semantic")

    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=True)
    config = _config(repos=["acme/widgets"], semantic={"enabled": False})
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(config, idx, cfg=cfg, embed_fn=_poison)
    assert code == 0
    assert idx.calls == ["acme/widgets"]
    assert idx.chunk_writer is None  # embedder path is dead -> no chunks
    messages = [r.getMessage() for r in caplog.records if r.name == "indexer.job"]
    assert "config.yaml semantic overrides applied: ['semantic_enabled']" in messages


@pytest.mark.unit
def test_config_yaml_semantic_enabled_beats_env_disabled() -> None:
    """The mirror of the disable case: ``semantic.enabled: true`` overlays an
    env-disabled cfg, so the embedder path goes LIVE.

    With cfg saying disabled, the un-overlaid run would pass no chunk_writer; the
    overlay flips ``cfg.semantic_enabled`` to True before the embedder wiring, so
    the injected embed_fn is used and a chunk_writer reaches index_repo.
    """
    idx = _RecordingIndex()
    embed_calls: list[list[str]] = []

    def _embed(texts: list[str]) -> list[list[float]]:
        embed_calls.append(list(texts))
        return [[0.5] for _ in texts]

    cfg = Settings(semantic_enabled=False)
    config = _config(repos=["acme/widgets"], semantic={"enabled": True})
    code = _run(config, idx, cfg=cfg, embed_fn=_embed)
    assert code == 0
    assert idx.chunk_writer is not None  # semantic went live despite the env-disabled cfg
    assert len(embed_calls) == 1  # the injected embedder actually ran


@pytest.mark.unit
def test_config_yaml_semantic_enabled_applies_the_worker_clamp(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The enable overlay also re-arms the 4-worker memory clamp (issue #109).

    cfg says disabled (which would leave the pool at index_concurrency=6), but
    config.yaml's ``semantic.enabled: true`` overlays before effective_workers, so
    the clamp fires: pool 6 -> 4, with the clamp log line -- the pool-side mirror
    of the disable test.
    """
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        kwargs = _engine_kwargs(
            _config(repos=["acme/widgets"], index_concurrency=6, semantic={"enabled": True}),
            monkeypatch,
            cfg=Settings(semantic_enabled=False),
        )
    assert kwargs["pool_size"] == 4  # clamped
    assert "clamping index_concurrency 6 -> 4" in caplog.text


@pytest.mark.unit
def test_config_yaml_semantic_global_cap_is_used_when_no_per_repo_override_matches() -> None:
    """The global cap from config.yaml's ``semantic:`` block feeds the effective cap.

    Same 2-chunk fixture (main.py + README.md). The injected cfg's cap of 1 would
    degrade the repo to a chunkless core index; the config.yaml global of 5 does not
    -- so chunk_writer being wired proves the OVERLAID global reached the precompute,
    not the env-backed cfg value.
    """
    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=1)
    config = _config(repos=["acme/widgets"], semantic={"max_chunks_per_repo": 5})
    code = _run(config, idx, cfg=cfg, embed_fn=lambda texts: [[0.0] for _ in texts])
    assert code == 0
    assert idx.calls == ["acme/widgets"]
    assert idx.chunk_writer is not None  # the config.yaml global (5), not cfg's 1, applied


@pytest.mark.unit
def test_per_repo_override_still_beats_the_config_yaml_global_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The per-repo MAP wins over the ``semantic:`` global, from whatever source.

    The job resolves ``entry.semantic_max_chunks or cfg.semantic_max_chunks_per_repo``;
    the ``semantic:`` block only moves the second operand. Here the config.yaml global
    (5) would pass the 2-chunk fixture, but the per-repo override (1) fails it -> the
    repo degrades to a chunkless core index, and the override log reports the global as
    5 (the overlaid value), proving both surfaces landed.
    """
    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=1)
    config = _config(
        repos=["acme/widgets"],
        semantic={"max_chunks_per_repo": 5},
        semantic_max_chunks_per_repo={"acme/widgets": 1},
    )
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(config, idx, cfg=cfg, embed_fn=lambda texts: [[0.0] for _ in texts])
    assert code == 0
    assert idx.calls == ["acme/widgets"]
    assert idx.chunk_writer is None  # the per-repo override (1) fired, not the global (5)
    messages = [r.getMessage() for r in caplog.records if r.name == "indexer.job"]
    assert "semantic chunk cap override for acme/widgets: 1 (global 5)" in messages


@pytest.mark.unit
def test_absent_semantic_block_leaves_injected_cfg_untouched(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No ``semantic:`` block -> no model_copy, no overlay log, cfg used verbatim.

    The injected cfg's cap of 1 stays in force (2-chunk fixture degrades to a
    chunkless core index), and the applied-overrides line is never emitted -- the
    regression guard that an empty overlay does not silently rebuild cfg.
    """
    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=1)
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            idx,
            cfg=cfg,
            embed_fn=lambda texts: [[0.0] for _ in texts],
        )
    assert code == 0
    assert idx.chunk_writer is None  # cfg's cap of 1 still enforced -> chunks skipped
    messages = [r.getMessage() for r in caplog.records if r.name == "indexer.job"]
    assert not any(m.startswith("config.yaml semantic overrides applied") for m in messages)


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


@pytest.mark.unit
def test_main_installs_the_repo_filter_on_every_root_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``[%(repo)s]`` format and ``RepoLogFilter`` are MUTUALLY load-bearing.

    ``main()`` sets a format referencing ``%(repo)s`` and installs the filter that
    supplies that attribute. Ship one without the other and every record raises
    ``KeyError: 'repo'`` inside logging, printing ``--- Logging error ---`` to
    stderr and DROPPING the message -- a total observability outage on a job whose
    only interface is its logs.

    Both halves must be pinned by ONE render. Note ``root.handlers`` starts EMPTY:
    ``logging.basicConfig`` is a documented no-op when the root logger already has
    handlers and ``main()`` passes no ``force=True``, so seeding a handler here
    would leave logging's DEFAULT formatter in place -- and the render assertion
    would then pass whether or not ``[%(repo)s]`` survived in main's format
    string. An earlier version of this test made exactly that mistake and
    verified only the filter half while claiming both.
    """
    import logging

    import indexer.job as job

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        root.handlers = []  # let basicConfig actually configure -- see docstring

        argv = ["code-search-index", "--config", "/c.yaml"]
        monkeypatch.setattr(job.sys, "argv", [*argv, "--scope", "s", "--key", "k"])
        monkeypatch.setattr(job, "run", lambda **_: 0)
        job.main()

        assert root.handlers, "main() left the root logger with no handlers"
        for handler in root.handlers:
            assert any(isinstance(f, job.RepoLogFilter) for f in handler.filters), (
                f"{handler!r} has no RepoLogFilter: every record would KeyError on %(repo)s"
            )
            record = logging.LogRecord(
                name="indexer.fetch",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="hello",
                args=(),
                exc_info=None,
            )
            # Render INSIDE a repo context and assert the repo NAME, not "[-]".
            # Asserting "[-]" would survive someone replacing %(repo)s with a
            # literal dash -- contrived, but then every production record would
            # read [-] forever and this test would stay green. The repo name
            # pins SUBSTITUTION, not just the presence of brackets.
            token = job._repo_ctx.set("acme/widgets")
            try:
                for filt in handler.filters:
                    filt.filter(record)
                rendered = handler.format(record)
            finally:
                job._repo_ctx.reset(token)
            # Filter half: no KeyError, message survives.
            assert "hello" in rendered
            # Format half: '[%(repo)s]' really is in main()'s format string.
            assert "[acme/widgets]" in rendered, (
                f"'[%(repo)s]' missing or not substituted in main()'s format: {rendered!r}"
            )
    finally:
        root.handlers, root.level = saved_handlers, saved_level


# --- Step 4: skip-if-unchanged ----------------------------------------------
# The stamp is (last_indexed_commit, index_semantics_version). A repo is skipped
# only when BOTH halves match — the SHA proves the content is current, the
# version proves it was produced by today's extraction semantics. _GitHub's
# resolve_ref answers sha_{repo}, so "sha_widgets" is acme/widgets' HEAD.


@pytest.mark.unit
def test_run_skips_a_repo_already_indexed_at_head() -> None:
    """The skip happens BEFORE the tarball: no download, no index_fn call."""
    idx = _RecordingIndex()
    engine = _FakeEngine(
        stamps={("acme/widgets", "main"): ("sha_widgets", INDEX_SEMANTICS_VERSION)}
    )
    github = _GitHub()
    code = _run(_config(repos=["acme/widgets"]), idx, github=github, engine=engine)
    assert code == 0
    assert idx.calls == []
    assert github.tarball_requests == []


@pytest.mark.unit
def test_run_reindexes_when_the_semantics_version_is_stale() -> None:
    """Same SHA, older semantics version -> re-index.

    This is the whole point of the version column: a change to symbol extraction
    must re-index a corpus whose commits have not moved.
    """
    idx = _RecordingIndex()
    engine = _FakeEngine(
        stamps={("acme/widgets", "main"): ("sha_widgets", INDEX_SEMANTICS_VERSION - 1)}
    )
    code = _run(_config(repos=["acme/widgets"]), idx, engine=engine)
    assert code == 0
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_run_reindexes_when_the_semantics_version_is_null() -> None:
    """NULL version = provenance unknown -> always re-index.

    This is the documented force-reindex escape hatch
    (``UPDATE repos SET index_semantics_version = NULL``); there is deliberately
    no ``--force_reindex`` flag.
    """
    idx = _RecordingIndex()
    engine = _FakeEngine(stamps={("acme/widgets", "main"): ("sha_widgets", None)})
    code = _run(_config(repos=["acme/widgets"]), idx, engine=engine)
    assert code == 0
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_run_reindexes_when_head_has_moved() -> None:
    idx = _RecordingIndex()
    engine = _FakeEngine(stamps={("acme/widgets", "main"): ("sha_old", INDEX_SEMANTICS_VERSION)})
    code = _run(_config(repos=["acme/widgets"]), idx, engine=engine)
    assert code == 0
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_run_indexes_a_repo_with_no_stamp_at_all() -> None:
    """An unknown repo gets stamp (None, None), which can never equal HEAD."""
    idx = _RecordingIndex()
    engine = _FakeEngine(stamps={("other/thing", "main"): ("sha_widgets", INDEX_SEMANTICS_VERSION)})
    code = _run(_config(repos=["acme/widgets"]), idx, engine=engine)
    assert code == 0
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_stamp_read_is_a_single_query_bounded_by_an_in_clause() -> None:
    """One SELECT for the whole run, filtered to the resolved entries.

    The ``IN`` clause is load-bearing: without it the read is bounded by the
    *table*, and ``repos`` keeps a row for every repo ever configured.
    """
    idx = _RecordingIndex()
    engine = _FakeEngine()
    code = _run(_config(repos=["acme/widgets", "acme/gadgets"]), idx, engine=engine)
    assert code == 0
    # Exactly one STAMPS query (this test's subject); the reconciliation
    # checkpoint's separate stored-repo-names SELECT is asserted elsewhere.
    stamp_queries = [stmt for stmt in engine.executed if "repo_branches" in str(stmt)]
    assert len(stamp_queries) == 1
    sql = str(stamp_queries[0])
    assert "repos.name IN " in sql
    assert "repo_branches.last_indexed_commit" in sql
    assert "repo_branches.index_semantics_version" in sql


# --- Multi-branch: branches: globs fan out into >1 branch per repo ---------


@pytest.mark.unit
def test_no_branches_configured_never_calls_the_branches_endpoint() -> None:
    """The common case costs no extra GitHub call -- resolve_branches ignores it anyway."""
    idx = _RecordingIndex()
    github = _GitHub()
    code = _run(_config(repos=["acme/widgets"]), idx, github=github)
    assert code == 0
    assert not any(p.endswith("/branches") for p in github.paths)
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_branches_glob_resolves_and_indexes_every_matching_branch() -> None:
    """A connection with branches: configured indexes every matching branch, sequentially."""
    idx = _RecordingIndex()
    github = _GitHub(branches={"acme/widgets": ["main", "release-1", "release-2", "dev"]})
    code = _run(_config(repos=["acme/widgets"], branches=["release-*"]), idx, github=github)
    assert code == 0
    # main (always included) + the two release-* matches; "dev" is not indexed.
    assert idx.calls == ["acme/widgets", "acme/widgets", "acme/widgets"]
    assert len([p for p in github.paths if p.endswith("/branches")]) == 1


@pytest.mark.unit
def test_per_branch_skip_is_independent_within_one_repo() -> None:
    """One branch already at HEAD is skipped; a divergent branch in the same repo is indexed."""
    idx = _RecordingIndex()
    github = _GitHub(branches={"acme/widgets": ["main", "feature"]})
    # commits/main -> sha_widgets, commits/feature -> sha_widgets_feature (see _GitHub).
    engine = _FakeEngine(
        stamps={("acme/widgets", "feature"): ("sha_widgets_feature", INDEX_SEMANTICS_VERSION)}
    )
    code = _run(
        _config(repos=["acme/widgets"], branches=["feature"]), idx, github=github, engine=engine
    )
    assert code == 0
    # "feature" was already current and skipped; only "main" reached index_fn.
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_one_branchs_conflict_does_not_stop_the_repos_other_branches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A StaleIndexError on one branch is isolated -- the repo's other branches still run."""
    github = _GitHub(branches={"acme/widgets": ["main", "feature"]})
    seen: list[str] = []

    def _index(conn: Any, *, name: str, branch: str, **_: Any) -> IndexCounts:
        seen.append(branch)
        if branch == "main":
            raise StaleIndexError(f"repo_branches row for {name}@{branch} changed")
        return IndexCounts(files=1, symbols=0, swept=0, edges=0)

    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"], branches=["feature"]), _index, github=github)

    assert code == 0  # a conflict alone does not fail the run
    # Both branches were attempted -- "main" conflicting did not skip "feature".
    assert sorted(seen) == ["feature", "main"]
    assert "index conflict for acme/widgets@main" in caplog.text
    assert "indexed acme/widgets@feature" in caplog.text


@pytest.mark.unit
def test_one_branchs_failure_does_not_stop_the_repos_other_branches() -> None:
    """Same isolation for a generic failure, not just a StaleIndexError conflict."""
    github = _GitHub(branches={"acme/widgets": ["main", "feature"]})
    seen: list[str] = []

    def _index(conn: Any, *, name: str, branch: str, **_: Any) -> IndexCounts:
        seen.append(branch)
        if branch == "main":
            raise RuntimeError("boom")
        return IndexCounts(files=1, symbols=0, swept=0, edges=0)

    code = _run(_config(repos=["acme/widgets"], branches=["feature"]), _index, github=github)

    assert code == 1  # a genuine failure DOES fail the run
    assert sorted(seen) == ["feature", "main"]


# --- reconciliation-safe branch discovery (BranchResolution / RepoOutcome) ---


@pytest.mark.unit
def test_index_one_inner_reports_discovery_complete_for_a_normal_run() -> None:
    """No branches: configured -> the fast path, always complete."""
    from indexer.job import _index_one_inner
    from indexer.resolve import RepoEntry

    with httpx.Client(transport=httpx.MockTransport(_GitHub())) as client:
        outcome = _index_one_inner(
            RepoEntry(name="acme/widgets", branch_globs=frozenset()),
            started=time.monotonic(),
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_RecordingIndex(),
            cfg=Settings(semantic_enabled=False),
            embed_fn=None,
            stamps={},
            shas_fn=_noop_shas_fn,
            extraction_pool=ExtractionPool(n_processes=0),
        )
    assert outcome.name == "acme/widgets"
    assert outcome.discovery_complete is True
    assert [o.branch for o in outcome.outcomes] == ["main"]


@pytest.mark.unit
def test_index_one_inner_reports_discovery_incomplete_when_capped() -> None:
    """More matching branches than SOFT_BRANCH_CAP -> discovery_complete is False."""
    from indexer.branches import SOFT_BRANCH_CAP
    from indexer.job import _index_one_inner
    from indexer.resolve import RepoEntry

    extra = [f"b{i:02d}" for i in range(SOFT_BRANCH_CAP + 5)]
    github = _GitHub(branches={"acme/widgets": ["main", *extra]})
    idx = _RecordingIndex()
    with httpx.Client(transport=httpx.MockTransport(github)) as client:
        outcome = _index_one_inner(
            RepoEntry(name="acme/widgets", branch_globs=frozenset({"*"})),
            started=time.monotonic(),
            http_client=client,
            engine=_FakeEngine(),
            index_fn=idx,
            cfg=Settings(semantic_enabled=False),
            embed_fn=None,
            stamps={},
            shas_fn=_noop_shas_fn,
            extraction_pool=ExtractionPool(n_processes=0),
        )
    assert outcome.discovery_complete is False
    assert len(outcome.outcomes) == SOFT_BRANCH_CAP


@pytest.mark.unit
def test_index_one_inner_default_flip_mirror_is_complete() -> None:
    """Mirrors the branch-level test: an unmatched retired branch does not affect completeness.

    Distinct from the cap-overflow case above: here nothing was dropped by the cap
    -- "master" simply never matched the glob -- so completeness stays True.
    """
    from indexer.job import _index_one_inner
    from indexer.resolve import RepoEntry

    github = _GitHub(branches={"acme/widgets": ["main", "master"]})
    idx = _RecordingIndex()
    with httpx.Client(transport=httpx.MockTransport(github)) as client:
        outcome = _index_one_inner(
            RepoEntry(name="acme/widgets", branch_globs=frozenset({"main"})),
            started=time.monotonic(),
            http_client=client,
            engine=_FakeEngine(),
            index_fn=idx,
            cfg=Settings(semantic_enabled=False),
            embed_fn=None,
            stamps={},
            shas_fn=_noop_shas_fn,
            extraction_pool=ExtractionPool(n_processes=0),
        )
    assert [o.branch for o in outcome.outcomes] == ["main"]
    assert outcome.discovery_complete is True


@pytest.mark.unit
def test_run_indexes_kept_branches_and_logs_reconciliation_blocked_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Truncation is not a failure: the run still exits 0 and indexes the kept branches,
    while the reconciliation-blocked warning is still visible in the run's own log."""
    from indexer.branches import SOFT_BRANCH_CAP

    extra = [f"b{i:02d}" for i in range(SOFT_BRANCH_CAP + 5)]
    idx = _RecordingIndex()
    github = _GitHub(branches={"acme/widgets": ["main", *extra]})
    with caplog.at_level(logging.WARNING, logger="indexer.branches"):
        code = _run(_config(repos=["acme/widgets"], branches=["*"]), idx, github=github)
    assert code == 0
    assert len(idx.calls) == SOFT_BRANCH_CAP  # truncated set still gets indexed

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name == "indexer.branches"
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "acme/widgets" in message
    assert "incomplete" in message.lower()
    assert "reconciliation" in message.lower()
    assert "blocked" in message.lower()


@pytest.mark.unit
def test_branch_listing_failure_fails_the_repo_and_never_calls_index_fn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A branch-listing API failure must fail the WHOLE repo, never return a
    silently short branch list -- a truncated-but-unflagged list would be wrongly
    trusted as complete reconciliation evidence."""
    idx = _RecordingIndex()
    github = _GitHub(branches_fail={"acme/widgets"})
    with caplog.at_level(logging.ERROR, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"], branches=["*"]), idx, github=github)
    assert code == 1
    assert idx.calls == []
    assert github.tarball_requests == []
    assert "failed to index acme/widgets" in caplog.text


# --- Step 5: bounded fan-out ------------------------------------------------


class _BarrierIndex:
    """index_fn that blocks until ``parties`` workers are inside it at once.

    The explicit ``timeout`` is mandatory, not defensive: a bare ``wait()`` with
    fewer workers than parties blocks forever, and ``shutdown(wait=True)`` would
    then HANG the suite instead of failing it.
    """

    def __init__(self, parties: int, timeout: float = 5.0) -> None:
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self.calls: list[str] = []

    def __call__(self, conn: Any, *, name: str, items: Any, **_: Any) -> IndexCounts:
        list(items)
        self.barrier.wait()
        self.calls.append(name)
        return IndexCounts(files=0, symbols=0, swept=0, edges=0)


@pytest.mark.unit
def test_two_repos_are_indexed_concurrently_at_concurrency_two() -> None:
    """Concurrency proof: both workers must be inside index_fn simultaneously.

    NOTE this proves CONCURRENCY, not throughput. It would pass identically in a
    world where fan-out delivers zero speedup — symbol extraction itself measured
    0.95x on 4 THREADS, which is exactly why extraction now runs in a shared
    process pool instead (#108) rather than leaning on this thread-level
    fan-out for it. Throughput is measured on the first production run, via
    the duration log lines asserted below.
    """
    idx = _BarrierIndex(parties=2)
    code = _run(_config(repos=["acme/widgets", "acme/gadgets"], index_concurrency=2), idx)
    assert code == 0
    assert sorted(idx.calls) == ["acme/gadgets", "acme/widgets"]


@pytest.mark.unit
def test_the_same_barrier_breaks_at_concurrency_one() -> None:
    """The negative half: without it, the test above proves nothing.

    At one worker the barrier can never trip, so it breaks on its timeout, each
    repo is counted a failure, and the run exits 1.
    """
    idx = _BarrierIndex(parties=2, timeout=0.5)
    code = _run(_config(repos=["acme/widgets", "acme/gadgets"], index_concurrency=1), idx)
    assert code == 1
    assert idx.calls == []


@pytest.mark.unit
def test_conflicts_only_run_exits_zero_without_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A StaleIndexError means the repos row changed under us: THIS REPO IS NOT
    INDEXED, because the whole transaction rolled back.

    It is excluded from the exit code because it SELF-HEALS -- whatever displaced
    the stamp makes the next run re-index this repo unconditionally. That is the
    rationale, NOT that the work was redundant and NOT that the index is already
    correct: for one run, that repo is stale.

    No claim is made here about WHAT displaces the stamp. Two earlier revisions
    of this docstring each named a cause and each was wrong -- first "another
    writer committed equally valid data", then "an operator running the
    force-reindex mid-run". Both are refuted by the row lock index_repo's
    statement 1 takes and holds to commit (measured against real Postgres: a
    concurrent force-reindex blocks, and the guard matches). This test pins the
    HANDLING -- own bucket, WARNING, no traceback, exit 0, honest message -- and
    deliberately asserts nothing about reachability, which belongs with the
    invariant in indexer/store.py.
    """

    def _conflict(conn: Any, *, name: str, items: Any, **_: Any) -> IndexCounts:
        list(items)
        raise StaleIndexError(f"repos row for {name} changed mid-transaction")

    with caplog.at_level(logging.DEBUG, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets", "acme/gadgets"]), _conflict)
    assert code == 0

    conflicts = [r for r in caplog.records if "index conflict for" in r.getMessage()]
    assert len(conflicts) == 2
    assert all(r.levelno == logging.WARNING for r in conflicts)
    assert all(r.exc_info is None for r in conflicts)
    assert "2 conflicts" in caplog.text
    # Pin the wording, not just the prefix. The whole point of exiting 0 here is
    # that the operator is told what actually happened; a message that reads as
    # "redundant work" (as an earlier revision did) makes exit-0 misleading.
    assert all("rolled back, not indexed" in r.getMessage() for r in conflicts)


@pytest.mark.unit
def test_mixed_run_accounts_for_every_repo(caplog: pytest.LogCaptureFixture) -> None:
    """ok + skipped + conflicts + failed == len(entries), reported in one line."""

    def _mixed(conn: Any, *, name: str, items: Any, **_: Any) -> IndexCounts:
        list(items)
        if name == "acme/conflicted":
            raise StaleIndexError(f"repos row for {name} changed mid-transaction")
        return IndexCounts(files=1, symbols=0, swept=0, edges=0)

    engine = _FakeEngine(
        stamps={("acme/skipped", "main"): ("sha_skipped", INDEX_SEMANTICS_VERSION)}
    )
    github = _GitHub(missing={"acme/broken"})
    entries = ["acme/widgets", "acme/skipped", "acme/conflicted", "acme/broken"]
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=entries), _mixed, github=github, engine=engine)
    assert code == 1  # the failure, not the conflict, is what fails the run

    completion = [r for r in caplog.records if "indexing complete" in r.getMessage()]
    assert len(completion) == 1
    message = completion[0].getMessage()
    assert "1 branch(es) ok, 1 skipped, 1 conflicts, 1 failed (across 4 repos)" in message


@pytest.mark.unit
def test_engine_is_disposed_only_after_every_worker_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pool nests inside the try, so shutdown(wait=True) joins before finally.

    A worker outliving engine.dispose() would use a disposed pool — asserted on
    timestamps rather than on absence of an exception, which a fake would not
    raise anyway.

    ``engine=None`` is required: run() only disposes an engine it OWNS, so the
    injected-engine path every other test uses would make this vacuous.
    """
    import indexer.job as job

    finished: list[float] = []

    def _slow(conn: Any, *, name: str, items: Any, **_: Any) -> IndexCounts:
        list(items)
        time.sleep(0.05)
        finished.append(time.monotonic())
        return IndexCounts(files=0, symbols=0, swept=0, edges=0)

    engine = _FakeEngine()
    monkeypatch.setattr(job, "create_db_engine", lambda **_kw: engine)
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
            index_fn=_slow,
            config_loader=lambda _c, _p: _config(
                repos=["acme/widgets", "acme/gadgets"], index_concurrency=2
            ),
            reconcile_retired_fn=_noop_retired_fn,
            reconcile_removed_fn=_noop_removed_fn,
        )
    assert code == 0
    assert engine.disposed is True
    assert len(finished) == 2
    assert engine.disposed_at is not None
    assert engine.disposed_at >= max(finished)


@pytest.mark.unit
def test_duration_is_logged_per_repo_and_for_the_whole_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A duration-logging instrument. Without these lines, "throughput is measured
    on the first production run" is an unfalsifiable promise, so the numbers are
    asserted to be present and parseable — not merely assumed."""
    idx = _RecordingIndex()
    engine = _FakeEngine(
        stamps={("acme/gadgets", "main"): ("sha_gadgets", INDEX_SEMANTICS_VERSION)}
    )
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets", "acme/gadgets"]), idx, engine=engine)
    assert code == 0

    indexed = _elapsed(caplog, r"finished acme/widgets in ([0-9.]+)s")
    skipped = _elapsed(caplog, r"skipped acme/gadgets@main: .* in ([0-9.]+)s")
    total = _elapsed(caplog, r"indexing complete: .* in ([0-9.]+)s")
    for elapsed in (indexed, skipped, total):
        assert elapsed >= 0.0


def _elapsed(caplog: pytest.LogCaptureFixture, pattern: str) -> float:
    """The single elapsed-seconds number matched by ``pattern``, as a float."""
    matches = [m for r in caplog.records if (m := re.search(pattern, r.getMessage()))]
    assert len(matches) == 1, f"expected exactly one {pattern!r} record, got {len(matches)}"
    return float(matches[0].group(1))


# --- Step 5: per-repo log context -------------------------------------------
# Via contextvars + a logging filter rather than hand-prefixing each call site,
# so records from indexer.fetch / indexer.store / app.embed — which carry no
# repo name of their own and WILL interleave under fan-out — are covered too.


@pytest.mark.unit
def test_records_from_other_modules_inherit_the_repo_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole point of the filter: a logger that knows nothing about repos."""
    import indexer.job as job

    def _logs_elsewhere(conn: Any, *, name: str, items: Any, **_: Any) -> IndexCounts:
        list(items)
        logging.getLogger("indexer.store").warning("a record from another module")
        return IndexCounts(files=0, symbols=0, swept=0, edges=0)

    log_filter = job.RepoLogFilter()
    caplog.handler.addFilter(log_filter)
    try:
        with caplog.at_level(logging.DEBUG):
            code = _run(_config(repos=["acme/widgets"]), _logs_elsewhere)
    finally:
        caplog.handler.removeFilter(log_filter)
    assert code == 0

    foreign = [r for r in caplog.records if r.name == "indexer.store"]
    assert len(foreign) == 1
    assert foreign[0].repo == "acme/widgets"

    # The main-thread drain loop is NOT inside any repo's context — proof the
    # worker's context did not leak out of the pool.
    completion = [r for r in caplog.records if "indexing complete" in r.getMessage()]
    assert completion[0].repo == "-"


@pytest.mark.unit
def test_repo_context_is_reset_even_when_the_repo_fails() -> None:
    """The ``finally: reset(token)`` is mandatory, not tidiness.

    ThreadPoolExecutor reuses worker threads and does NOT reset the context
    between tasks. Without the reset, any record emitted between task N's
    completion and task N+1's set() — including a failure raised before that
    set() — would be attributed to the PREVIOUS repo.
    """
    from indexer.job import _index_one, _repo_ctx
    from indexer.resolve import RepoEntry

    assert _repo_ctx.get() == "-"
    with (
        httpx.Client(transport=httpx.MockTransport(_GitHub())) as client,
        pytest.raises(ValueError),
    ):
        # A malformed entry: normalize_repo raises AFTER the context is set.
        _index_one(
            RepoEntry(name="https://evil.com/a/b", branch_globs=frozenset()),
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_RecordingIndex(),
            cfg=Settings(semantic_enabled=False),
            embed_fn=None,
            stamps={},
            shas_fn=_noop_shas_fn,
            extraction_pool=ExtractionPool(n_processes=0),
        )
    assert _repo_ctx.get() == "-"


# --- Carry-forward from Step 0: determinism ACROSS index_concurrency ---------
# Step 0 proved determinism only at the THREAD level (test_symbols.py), because
# index_concurrency did not exist yet. This closes it end to end: the same corpus
# indexed at 1 worker and at 4 must yield byte-identical symbol tuples.

_CORPUS = {
    "alpha.py": b"class A:\n    def one(self):\n        return 1\n\n"
    b"def top_alpha():\n    return A\n",
    "beta.py": b"def b1():\n    pass\n\n\ndef b2():\n    pass\n\n\nclass B:\n    pass\n",
    "gamma.py": b"class G:\n    def g_method(self):\n        def inner():\n"
    b"            return 0\n        return inner\n",
    "README.md": b"# corpus\n",
}


class _SymbolCollector:
    """Records every (repo, path, symbol) tuple index_fn is handed."""

    def __init__(self) -> None:
        # Appended to from worker threads; list.append is atomic, and the test
        # sorts before comparing precisely because the ORDER is not a contract.
        self.rows: list[tuple[str, str, str, str, int, int]] = []

    def __call__(self, conn: Any, *, name: str, items: Any, **_: Any) -> IndexCounts:
        collected: list[tuple[str, str, str, str, int, int]] = []
        for pf, ex in items:
            for sym in ex.symbols:
                assert isinstance(sym, ExtractedSymbol)
                collected.append((name, pf.path, sym.name, sym.kind, sym.start_line, sym.end_line))
        self.rows.extend(collected)
        return IndexCounts(files=0, symbols=len(collected), swept=0, edges=0)

    @property
    def sorted_rows(self) -> list[tuple[str, str, str, str, int, int]]:
        return sorted(self.rows)


@pytest.mark.unit
def test_symbols_are_identical_at_concurrency_one_and_four() -> None:
    repos = ["acme/widgets", "acme/gadgets", "acme/sprockets", "acme/cogs"]

    def _index_at(concurrency: int) -> list[tuple[str, str, str, str, int, int]]:
        collector = _SymbolCollector()
        code = _run(
            _config(repos=repos, index_concurrency=concurrency),
            collector,
            github=_GitHub(files=_CORPUS),
        )
        assert code == 0
        return collector.sorted_rows

    serial = _index_at(1)
    parallel = _index_at(4)
    # Sanity: the corpus really does produce symbols, so equality is not vacuous.
    # 9 per repo: 3 in alpha.py, 3 in beta.py, 3 in gamma.py (README.md yields none).
    assert len(serial) == len(repos) * 9
    assert serial == parallel


# --- Step 6: the pool is derived from the worker count ----------------------


def _engine_kwargs(
    config: RepoConfig, monkeypatch: pytest.MonkeyPatch, *, cfg: Settings | None = None
) -> dict[str, Any]:
    """Run with ``engine=None`` and return the kwargs create_db_engine got.

    ``engine=None`` is load-bearing: every other test injects an engine, and on
    that path create_db_engine is never called at all.
    """
    import indexer.job as job

    recorded: list[dict[str, Any]] = []

    def _record(**kw: Any) -> _FakeEngine:
        recorded.append(kw)
        return _FakeEngine()

    monkeypatch.setattr(job, "create_db_engine", _record)
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
            index_fn=_RecordingIndex(),
            cfg=cfg,
            config_loader=lambda _c, _p: config,
            reconcile_retired_fn=_noop_retired_fn,
            reconcile_removed_fn=_noop_removed_fn,
        )
    assert code == 0
    assert len(recorded) == 1
    return recorded[0]


@pytest.mark.unit
def test_pool_size_tracks_index_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    """One worker holds exactly one connection, so the pool is sized to the workers.

    ``max_overflow=0`` makes a connection leak a loud stall rather than silent
    pool growth; ``pool_timeout`` is passed explicitly — it is SQLAlchemy's own
    default, but spelling it at the call site is what makes the length of that
    stall readable next to the overflow ban that causes it.
    """
    kwargs = _engine_kwargs(
        _config(repos=["acme/widgets"], index_concurrency=6),
        monkeypatch,
        cfg=Settings(semantic_enabled=False),
    )
    assert kwargs["pool_size"] == 6
    assert kwargs["max_overflow"] == 0
    assert kwargs["pool_timeout"] == 30


@pytest.mark.unit
def test_pool_size_follows_the_semantic_clamp_not_the_raw_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The pool must track the EFFECTIVE workers, not index_concurrency.

    With semantic on, effective_workers clamps 6 -> 4 (issue #109); a pool of 6
    would then over-provision Lakebase connections that no worker can ever use.
    The clamp is also logged, because a run silently doing two-thirds of the
    requested concurrency is otherwise invisible.
    """
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        kwargs = _engine_kwargs(
            _config(repos=["acme/widgets"], index_concurrency=6),
            monkeypatch,
            cfg=Settings(semantic_enabled=True),
        )
    assert kwargs["pool_size"] == 4
    assert kwargs["max_overflow"] == 0
    assert "clamping index_concurrency 6 -> 4" in caplog.text


@pytest.mark.unit
def test_config_yaml_semantic_disabled_removes_the_worker_clamp(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The overlay runs BEFORE effective_workers, so config.yaml can lift the clamp.

    cfg says semantic enabled (which would clamp 6 -> 4), but config.yaml's
    ``semantic.enabled: false`` overlays first -- effective_workers then sees a
    disabled flag and leaves the pool at the full index_concurrency, with no clamp
    log line. This is the pool-side proof that the overlay precedes both the clamp
    and the embedder build.
    """
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        kwargs = _engine_kwargs(
            _config(repos=["acme/widgets"], index_concurrency=6, semantic={"enabled": False}),
            monkeypatch,
            cfg=Settings(semantic_enabled=True),
        )
    assert kwargs["pool_size"] == 6  # not clamped to 4
    assert "clamping index_concurrency" not in caplog.text


@pytest.mark.unit
def test_no_clamp_line_at_the_shipped_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """At `index_concurrency=4` (the shipped default) the clamp is a no-op.

    `effective_workers` returns `min(index_concurrency, 4)`, and the clamp log
    line only fires when it actually reduces the value (`workers !=
    config.index_concurrency`, job.py). So a default-configured, semantic-on run
    emits NO clamp line at all -- issue #109's runbook update explicitly documents
    this as confirmed on the live dev job's Arm B runs; this pins it in a unit
    test too.
    """
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        kwargs = _engine_kwargs(
            _config(repos=["acme/widgets"], index_concurrency=4),
            monkeypatch,
            cfg=Settings(semantic_enabled=True),
        )
    assert kwargs["pool_size"] == 4
    assert "clamping index_concurrency" not in caplog.text


@pytest.mark.unit
def test_shas_fn_connection_closes_before_embedding_starts() -> None:
    """E7 (issue #109): the advisory ``shas_fn`` connection is short-lived.

    ``pool_size == workers`` (the tests above) only holds because each worker
    opens at most ONE connection at a time -- by sequencing, not by
    construction. On the delta-gate-open path a worker opens a SECOND,
    short-lived connection (``with engine.connect() as shas_conn:``,
    ``indexer/job.py``) for ``shas_fn``, which must close before
    ``_precompute_chunk_writer``/embedding starts -- otherwise a single worker
    could hold 2 connections at once and pool_size==workers would
    under-provision. Wraps the fake engine's own ``connect()`` to count
    currently-open connections and samples that count from inside ``shas_fn``
    and ``embed_fn`` -- extending
    ``test_shas_fn_called_once_before_embedding_when_version_matches_and_sha_differs``'s
    pattern one step further.
    """
    open_count = 0
    open_during: dict[str, int] = {}

    engine = _FakeEngine(stamps={("acme/widgets", "main"): ("sha_old", INDEX_SEMANTICS_VERSION)})
    real_connect = engine.connect

    class _TrackingConn:
        def __init__(self, inner: _FakeConn) -> None:
            self._inner = inner

        def __enter__(self) -> _TrackingConn:
            nonlocal open_count
            open_count += 1
            self._inner.__enter__()
            return self

        def __exit__(self, *exc: Any) -> bool:
            nonlocal open_count
            open_count -= 1
            return self._inner.__exit__(*exc)

        def execute(self, stmt: Any) -> Any:
            return self._inner.execute(stmt)

        def rollback(self) -> None:
            self._inner.rollback()

    def _tracking_connect() -> _TrackingConn:
        return _TrackingConn(real_connect())

    engine.connect = _tracking_connect  # type: ignore[method-assign]

    def _shas_fn(conn: Any, *, name: str, branch: str) -> tuple[set[Any], set[Any]]:
        open_during["shas"] = open_count
        return set(), set()

    def _embed(texts: list[str]) -> list[list[float]]:
        open_during["embed"] = open_count
        return [[0.0] for _ in texts]

    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    idx = _RecordingIndex()
    code = _run(
        _config(repos=["acme/widgets"]),
        idx,
        cfg=cfg,
        embed_fn=_embed,
        engine=engine,
        shas_fn=_shas_fn,
    )
    assert code == 0
    # Guards against a vacuous pass: job.py swallows any semantic-precompute
    # failure and still returns 0 (the semantic layer is additive), which would
    # let a degraded run satisfy the connection-count assertion below without
    # ever really reaching the embed step it's supposed to pin.
    assert idx.chunk_writer is not None, "semantic precompute must have succeeded, not degraded"
    assert open_during == {"shas": 1, "embed": 0}, (
        "shas_fn's own connection must be open exactly while it runs, and fully "
        f"closed again before embedding starts (got {open_during!r})"
    )


@pytest.mark.unit
def test_indexer_reaches_no_hardcoded_pool_constant() -> None:
    """Tripwire: the indexer must never fall back to the server's pool default.

    ``app.db.client`` applies its ``_DEFAULT_POOL_SIZE`` only on the Lakebase
    branch — the local branch forwards **pool_kwargs raw — so a job that stopped
    passing ``pool_size`` would silently get a different pool in each mode.
    """
    import indexer.job as job

    source = inspect.getsource(job)
    assert "pool_size=workers" in source, "the pool must be derived from the worker count"
    assert "_POOL_SIZE" not in source, "the indexer must not reference a pool constant"


# --- Step 7: the disk headroom guard ----------------------------------------


@pytest.mark.unit
def test_run_logs_free_disk_at_start(caplog: pytest.LogCaptureFixture) -> None:
    """The serverless local-disk size is undocumented, so every run prints it.

    This is the measurement that lets the peak-usage arithmetic in the runbook
    be checked against reality instead of assumed.
    """
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), _RecordingIndex())
    assert code == 0

    pattern = r"local disk at .*: ([0-9.]+) GB free of ([0-9.]+) GB total"
    matches = [m for r in caplog.records if (m := re.search(pattern, r.getMessage()))]
    assert len(matches) == 1, "expected exactly one disk-usage record"
    free, total = float(matches[0].group(1)), float(matches[0].group(2))
    assert 0.0 <= free <= total


@pytest.mark.unit
def test_starved_repo_fails_alone_and_downloads_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A disk shortfall costs ONE repo, not the run — and costs it before any bytes.

    Per-repo isolation is what turns a too-high index_concurrency into "indexed
    whatever fits" rather than a whole-run outage, and the guard running before
    download_tarball is what stops the starved worker from making the shortage
    worse on its way to failing.
    """
    import indexer.job as job

    def _guard(path: Any, *, repo: str) -> None:
        if repo.startswith("acme/gadgets"):
            raise OSError(f"insufficient local disk for {repo}: lower index_concurrency")

    monkeypatch.setattr(job, "assert_disk_headroom", _guard)

    idx = _RecordingIndex()
    github = _GitHub()
    with caplog.at_level(logging.ERROR, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets", "acme/gadgets"]), idx, github=github)

    # The run fails (exit 1) but the healthy repo is still indexed.
    assert code == 1
    assert idx.calls == ["acme/widgets"]
    # Not one byte was fetched for the starved repo.
    assert github.tarball_requests == ["/repos/acme/widgets/tarball/sha_widgets"]
    assert "failed to index acme/gadgets" in caplog.text


# --- Reconciliation checkpoint -----------------------------------------------


def _repo_entry(name: str) -> RepoEntry:
    return RepoEntry(name=name, branch_globs=frozenset())


def _ok_outcome(
    name: str, *, branches: list[str] | None = None, discovery_complete: bool = True
) -> RepoOutcome:
    branches = branches or ["main"]
    return RepoOutcome(
        name=name,
        discovery_complete=discovery_complete,
        outcomes=[
            BranchOutcome(
                branch=b, status="indexed", counts=IndexCounts(files=1, symbols=0, swept=0, edges=0)
            )
            for b in branches
        ],
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "failures,conflicts,outcome_count,entry_count,discovery_complete,expected",
    [
        (0, 0, 1, 1, True, True),  # clean run: gate passes
        (1, 0, 1, 1, True, False),  # any failure blocks
        (0, 1, 1, 1, True, False),  # any conflict blocks
        (0, 0, 1, 2, True, False),  # fewer outcomes than entries blocks
        (0, 0, 1, 1, False, False),  # incomplete branch discovery blocks
    ],
)
def test_decide_reconciliation_truth_table(
    failures: int,
    conflicts: int,
    outcome_count: int,
    entry_count: int,
    discovery_complete: bool,
    expected: bool,
) -> None:
    entries = [_repo_entry(f"acme/r{i}") for i in range(entry_count)]
    repo_outcomes = [
        _ok_outcome(f"acme/r{i}", discovery_complete=discovery_complete)
        for i in range(outcome_count)
    ]
    assert (
        _decide_reconciliation(
            failures=failures,
            conflicts=conflicts,
            repo_outcomes=repo_outcomes,
            entries=entries,
        )
        is expected
    )


@pytest.mark.unit
def test_retires_only_persisted_minus_resolved_branches() -> None:
    """A stale persisted branch is retired; a branch still resolved this run is not.

    Also covers a first-ever repo (``acme/gadgets``, nothing persisted): it is
    never itself "retired" -- there is nothing in ``stamps`` to subtract from.
    """
    rec = _RecordingReconcile()
    engine = _FakeEngine(
        stamps={
            ("acme/widgets", "main"): ("sha_widgets", INDEX_SEMANTICS_VERSION),
            ("acme/widgets", "stale"): (None, None),
        }
    )
    code = _run(
        _config(repos=["acme/widgets", "acme/gadgets"]),
        _RecordingIndex(),
        engine=engine,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert code == 0
    assert rec.retired_calls == [("acme/widgets", ["stale"])]


@pytest.mark.unit
def test_skipped_branches_permit_reconciliation() -> None:
    rec = _RecordingReconcile()
    engine = _FakeEngine(
        stamps={("acme/widgets", "main"): ("sha_widgets", INDEX_SEMANTICS_VERSION)}
    )
    code = _run(
        _config(repos=["acme/widgets"]),
        _RecordingIndex(),
        engine=engine,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert code == 0  # the branch was skipped, not indexed -- still counts as success
    assert rec.removed_calls == [["acme/widgets"]]


@pytest.mark.unit
def test_desired_repos_passed_to_removed_fn_is_exact_and_sorted() -> None:
    rec = _RecordingReconcile()
    engine = _FakeEngine(
        stamps={("acme/widgets", "main"): ("sha_widgets", INDEX_SEMANTICS_VERSION)}
    )
    code = _run(
        _config(repos=["acme/widgets", "acme/gadgets"]),
        _RecordingIndex(),
        engine=engine,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert code == 0
    # acme/widgets' only outcome this run was "skipped" -- still in desired_repos.
    assert rec.removed_calls == [["acme/gadgets", "acme/widgets"]]


@pytest.mark.unit
def test_branch_failure_blocks_reconciliation() -> None:
    def _failing_index(conn: Any, *, name: str, items: Any, **_: Any) -> IndexCounts:
        list(items)
        raise RuntimeError("boom")

    rec = _RecordingReconcile()
    code = _run(
        _config(repos=["acme/widgets"]),
        _failing_index,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert code == 1
    assert rec.retired_calls == []
    assert rec.removed_calls == []


@pytest.mark.unit
def test_branch_conflict_blocks_reconciliation_but_exits_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def _conflicting_index(conn: Any, *, name: str, items: Any, **_: Any) -> IndexCounts:
        list(items)
        raise StaleIndexError("changed mid-transaction")

    rec = _RecordingReconcile()
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            _conflicting_index,
            reconcile_retired_fn=rec.retired_fn,
            reconcile_removed_fn=rec.removed_fn,
        )
    assert code == 0  # conflicts self-heal -- they don't fail the run
    assert rec.retired_calls == []
    assert rec.removed_calls == []
    lines = [
        r.getMessage() for r in caplog.records if "corpus reconciliation skipped" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "conflict" in lines[0]


@pytest.mark.unit
def test_repo_level_failure_blocks_reconciliation() -> None:
    github = _GitHub(missing={"acme/broken"})
    rec = _RecordingReconcile()
    code = _run(
        _config(repos=["acme/widgets", "acme/broken"]),
        _RecordingIndex(),
        github=github,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert code == 1
    assert rec.retired_calls == []
    assert rec.removed_calls == []


@pytest.mark.unit
def test_soft_cap_truncated_discovery_blocks_reconciliation() -> None:
    from indexer.branches import SOFT_BRANCH_CAP

    extra = [f"b{i:02d}" for i in range(SOFT_BRANCH_CAP + 5)]
    idx = _RecordingIndex()
    github = _GitHub(branches={"acme/widgets": ["main", *extra]})
    rec = _RecordingReconcile()
    code = _run(
        _config(repos=["acme/widgets"], branches=["*"]),
        idx,
        github=github,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert code == 0  # truncation alone does not fail the run
    assert rec.retired_calls == []
    assert rec.removed_calls == []  # but corpus-wide reconciliation is blocked


@pytest.mark.unit
def test_reconciliation_runs_after_fanout_drains() -> None:
    """An ordering probe: every index call happens before any reconcile call."""
    order: list[str] = []

    def _index_fn(conn: Any, *, name: str, items: Any, **_: Any) -> IndexCounts:
        list(items)
        order.append(f"index:{name}")
        return IndexCounts(files=1, symbols=0, swept=0, edges=0)

    class _OrderedReconcile(_RecordingReconcile):
        def removed_fn(self, conn: Any, *, desired_repos: Any) -> list[str]:
            order.append("removed")
            return super().removed_fn(conn, desired_repos=desired_repos)

    rec = _OrderedReconcile()
    code = _run(
        _config(repos=["acme/widgets", "acme/gadgets"]),
        _index_fn,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert code == 0
    index_positions = [i for i, e in enumerate(order) if e.startswith("index:")]
    assert len(index_positions) == 2
    assert max(index_positions) < order.index("removed")


@pytest.mark.unit
def test_reconcile_fns_never_referenced_from_worker_source() -> None:
    """Source-level tripwire: reconciliation is never reachable from worker-thread code."""
    import indexer.job as job

    worker_src = "".join(
        inspect.getsource(fn)
        for fn in (job._index_one, job._index_one_inner, job._index_one_branch)
    )
    forbidden = (
        "reconcile_retired_fn",
        "reconcile_removed_fn",
        "reconcile_retired_branches",
        "reconcile_removed_repos",
        "_reconcile(",
        "_decide_reconciliation",
    )
    for needle in forbidden:
        assert needle not in worker_src, f"{needle!r} referenced from worker-thread source"


@pytest.mark.unit
def test_reconciliation_complete_summary_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    rec = _RecordingReconcile(
        retired_result=ReconcileCounts(branches_removed=2, files_stripped=3, files_deleted=1),
        removed_result=["acme/old-repo"],
    )
    engine = _FakeEngine(stamps={("acme/widgets", "stale-branch"): (None, None)})
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            _RecordingIndex(),
            engine=engine,
            reconcile_retired_fn=rec.retired_fn,
            reconcile_removed_fn=rec.removed_fn,
        )
    assert code == 0
    assert rec.retired_calls == [("acme/widgets", ["stale-branch"])]
    lines = [
        r.getMessage() for r in caplog.records if "corpus reconciliation complete" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "2 branch(es) retired" in lines[0]
    assert "3 file(s) stripped" in lines[0]
    assert "1 deleted" in lines[0]
    assert "1 repo(s) purged" in lines[0]


@pytest.mark.unit
def test_reconciliation_skipped_summary_includes_reasons(caplog: pytest.LogCaptureFixture) -> None:
    github = _GitHub(missing={"acme/broken"})
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets", "acme/broken"]), _RecordingIndex(), github=github
        )
    assert code == 1
    lines = [
        r.getMessage() for r in caplog.records if "corpus reconciliation skipped" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "1 repo/branch failure(s)" in lines[0]
    assert "1 repo(s) never completed" in lines[0]


@pytest.mark.unit
def test_reconciliation_skip_reason_never_empty_when_gate_fails() -> None:
    """Companion unit check for _reconciliation_skip_reason directly (no run() plumbing)."""
    entries = [_repo_entry("acme/widgets")]
    reason = _reconciliation_skip_reason(failures=1, conflicts=0, repo_outcomes=[], entries=entries)
    assert "1 repo/branch failure(s)" in reason


@pytest.mark.unit
def test_committed_any_false_logs_left_stale(caplog: pytest.LogCaptureFixture) -> None:
    """Nothing committed before the failure -> "left stale", never "PARTIALLY"."""
    rec = _RecordingReconcile(removed_raises=RuntimeError("boom"))
    with caplog.at_level(logging.ERROR, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            _RecordingIndex(),
            reconcile_retired_fn=rec.retired_fn,
            reconcile_removed_fn=rec.removed_fn,
        )
    assert code == 1
    assert (
        rec.retired_calls == []
    )  # nothing to retire -> never called -> committed_any stayed False
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("left stale" in m for m in errors)
    assert not any("PARTIALLY" in m for m in errors)


@pytest.mark.unit
def test_committed_any_true_logs_partially_reconciled(caplog: pytest.LogCaptureFixture) -> None:
    """A commit before the failure -> "PARTIALLY reconciled", not "left stale"."""
    rec = _RecordingReconcile(removed_raises=RuntimeError("boom"))
    engine = _FakeEngine(stamps={("acme/widgets", "stale-branch"): (None, None)})
    with caplog.at_level(logging.ERROR, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            _RecordingIndex(),
            engine=engine,
            reconcile_retired_fn=rec.retired_fn,
            reconcile_removed_fn=rec.removed_fn,
        )
    assert code == 1
    assert rec.retired_calls == [("acme/widgets", ["stale-branch"])]  # committed before the raise
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("PARTIALLY reconciled" in m for m in errors)
    assert not any("left stale" in m for m in errors)


@pytest.mark.unit
def test_purge_shrink_guard_withholds_majority_purge(caplog: pytest.LogCaptureFixture) -> None:
    rec = _RecordingReconcile(
        retired_result=ReconcileCounts(branches_removed=1, files_stripped=1, files_deleted=0)
    )
    engine = _FakeEngine(
        stamps={("acme/a", "stale"): (None, None)},
        repo_names=["acme/a", "acme/b", "acme/c", "acme/d"],
    )
    with caplog.at_level(logging.ERROR, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/a"]),
            _RecordingIndex(),
            engine=engine,
            reconcile_retired_fn=rec.retired_fn,
            reconcile_removed_fn=rec.removed_fn,
        )
    assert code == 1
    # The surviving repo's OWN retired-branch cleanup still ran...
    assert rec.retired_calls == [("acme/a", ["stale"])]
    # ...but the corpus-wide purge was withheld entirely.
    assert rec.removed_calls == []
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    # The committed survivor cleanup is reported alongside the withhold, not just
    # the would-purge/stored counts -- an operator must not have to infer what
    # DID commit from a message that only says what was blocked.
    assert any(
        "withheld" in m and "3/4" in m and "1 branch(es)" in m and "1 file(s) stripped" in m
        for m in errors
    )


@pytest.mark.unit
def test_purge_shrink_guard_boundary_exactly_half_proceeds() -> None:
    """Strict ``>``: a purge removing EXACTLY half of stored repos is NOT withheld."""
    rec = _RecordingReconcile()
    engine = _FakeEngine(repo_names=["acme/a", "acme/b", "acme/c", "acme/d"])
    code = _run(
        _config(repos=["acme/a", "acme/b"]),
        _RecordingIndex(),
        engine=engine,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert code == 0
    assert rec.removed_calls == [["acme/a", "acme/b"]]


@pytest.mark.unit
def test_primitive_failure_logs_safe_fields_only(caplog: pytest.LogCaptureFixture) -> None:
    secret_message = "leak-marker-9f3c db=prod user=admin"
    rec = _RecordingReconcile(removed_raises=RuntimeError(secret_message))
    with caplog.at_level(logging.ERROR, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            _RecordingIndex(),
            reconcile_retired_fn=rec.retired_fn,
            reconcile_removed_fn=rec.removed_fn,
        )
    assert code == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("RuntimeError" in m for m in errors)
    assert not any(secret_message in m for m in errors)
    for record in caplog.records:
        assert secret_message not in record.getMessage()
        for arg in record.args or ():
            assert secret_message not in str(arg)


@pytest.mark.unit
def test_reconcile_fn_defaults_are_the_real_primitives() -> None:
    sig = inspect.signature(run)
    assert sig.parameters["reconcile_retired_fn"].default is reconcile_retired_branches
    assert sig.parameters["reconcile_removed_fn"].default is reconcile_removed_repos


@pytest.mark.unit
def test_retired_branch_names_are_never_casefolded() -> None:
    """``stamps`` keys are casefolded on the name component only -- never the branch."""
    rec = _RecordingReconcile()
    engine = _FakeEngine(stamps={("acme/widgets", "Release-3.2"): (None, None)})
    code = _run(
        _config(repos=["acme/widgets"]),
        _RecordingIndex(),
        engine=engine,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert code == 0
    assert rec.retired_calls == [("acme/widgets", ["Release-3.2"])]


@pytest.mark.unit
def test_reconcile_unit_level_partial_progress_on_mid_sequence_failure() -> None:
    """Unit-level (no run() plumbing): _reconcile itself returns an accurate partial progress."""
    rec = _RecordingReconcile(removed_raises=RuntimeError("boom"))
    repo_outcomes = [_ok_outcome("acme/widgets")]
    stamps = {("acme/widgets", "stale"): (None, None)}
    engine = _FakeEngine(stamps=stamps)
    progress, failed = _reconcile(
        engine,
        repo_outcomes=repo_outcomes,
        stamps=stamps,
        reconcile_retired_fn=rec.retired_fn,
        reconcile_removed_fn=rec.removed_fn,
    )
    assert failed is True
    assert progress.committed_any is True
    assert progress.purge_blocked is False
    assert progress.purged_repos == []


# --- streaming tarball ingestion (#106) -------------------------------------


def _two_top_level_dirs_tarball() -> bytes:
    """A real gzip tarball that ``indexer.ingest`` must REJECT.

    GitHub tarballs carry exactly one top-level ``org-repo-<sha7>/`` directory;
    two is the cheapest malformed shape that is still a structurally valid
    archive, so the failure comes from ingestion rather than from gzip.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("one/a.py", "two/b.py"):
            info = tarfile.TarInfo(name)
            info.size = len(b"x = 1\n")
            tf.addfile(info, io.BytesIO(b"x = 1\n"))
    return buf.getvalue()


class _ConnectCountingEngine(_FakeEngine):
    """``_FakeEngine`` that counts ``connect()`` calls.

    ``run()`` takes exactly one connection outside the workers (the pre-fan-out
    stamp read; the reconciliation checkpoint is gated off by any failure), so on
    a one-repo failing run the count is 1 plus whatever the branch itself took.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.connects = 0

    def connect(self) -> _FakeConn:
        self.connects += 1
        return super().connect()


@pytest.mark.unit
def test_no_file_is_written_beside_the_tarball(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2: the worker's temp dir holds the compressed tarball and nothing else.

    Asserted from inside ``index_fn`` -- i.e. while the branch is mid-flight and
    an extracted tree would still exist -- not after the ``TemporaryDirectory``
    has already torn itself down. The spy is on ``indexer.job.download_tarball``:
    ``job.py`` binds that name into its own namespace at import, so patching
    ``indexer.fetch.download_tarball`` would miss.
    """
    import indexer.job as job

    real_download_tarball = job.download_tarball
    dests: list[Path] = []

    def _download_tarball(*args: Any, **kwargs: Any) -> Any:
        out = real_download_tarball(*args, **kwargs)
        dests.append(out.parent)
        return out

    monkeypatch.setattr(job, "download_tarball", _download_tarball)

    snapshots: list[list[str]] = []

    def _index(conn: Any, *, items: Any, **_: Any) -> IndexCounts:
        consumed = list(items)
        snapshots.append(sorted(p.name for p in dests[-1].iterdir()))
        return IndexCounts(files=len(consumed), symbols=0, swept=0, edges=0)

    assert _run(_config(repos=["acme/widgets"]), _index) == 0
    assert snapshots == [["source.tar.gz"]]


@pytest.mark.unit
def test_malformed_archive_fails_the_branch_from_inside_the_transaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D8, semantic-OFF path: archive validation moved inside the open transaction.

    ``extract_tarball`` raised before ``engine.connect()``. The lazy generator is
    now consumed inside ``index_repo``'s ``conn.begin()``, so a malformed archive
    costs one pooled connection and a rolled-back transaction. Accepted because
    the branch-level OUTCOME is unchanged -- ``status="failed"``, exit code 1, no
    ``phase timing`` line -- and the blast radius is one worker's own connection
    (``pool_size == workers``, ``max_overflow=0``).
    """
    github = _GitHub(tarball_bytes=_two_top_level_dirs_tarball())
    engine = _ConnectCountingEngine()
    idx = _RecordingIndex()

    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            idx,
            cfg=Settings(semantic_enabled=False),
            github=github,
            engine=engine,
        )

    assert code == 1
    assert idx.counts == []  # the transaction never completed
    assert engine.connects == 2  # the pre-fan-out stamp read, plus this branch's
    assert any("failed to index acme/widgets@main" in r.getMessage() for r in caplog.records)
    assert _timing_messages(caplog) == []  # a failed branch emits no timing line


@pytest.mark.unit
def test_malformed_archive_on_the_semantic_path_raises_before_any_connection() -> None:
    """D8's twin: the PRODUCTION path still fails with zero connections held.

    On the semantic path ``job.py`` materializes the file list eagerly, so the
    archive is fully streamed and fully validated before #104's advisory
    ``shas_fn`` connection is opened and before ``index_repo``'s. The stamp below
    matches ``INDEX_SEMANTICS_VERSION`` with a stale SHA, which is exactly the
    shape that OPENS the delta gate -- so ``shas_fn`` would be called if the raise
    were even one step later.
    """
    github = _GitHub(tarball_bytes=_two_top_level_dirs_tarball())
    engine = _ConnectCountingEngine(
        stamps={("acme/widgets", "main"): ("stale_sha", INDEX_SEMANTICS_VERSION)}
    )
    idx = _RecordingIndex()
    shas_calls: list[tuple[str, str]] = []

    def _shas_fn(conn: Any, *, name: str, branch: str) -> tuple[set[Any], set[Any]]:
        shas_calls.append((name, branch))
        return set(), set()

    code = _run(
        _config(repos=["acme/widgets"]),
        idx,
        cfg=Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100),
        embed_fn=lambda texts: [[0.0] for _ in texts],
        github=github,
        engine=engine,
        shas_fn=_shas_fn,
    )

    assert code == 1
    assert idx.counts == []
    assert shas_calls == []
    assert engine.connects == 1  # the pre-fan-out stamp read alone


# --- per-phase timing instrumentation (#103) --------------------------------
# Every INDEXED branch emits one `phase timing` record accounting for its whole
# wall clock. The numbers are asserted EXACTLY, never with sleeps: the tests
# below patch `indexer.timing._CLOCK` (the single seam every asserted interval
# reads, captured by PhaseTimer.__init__ inside the worker) with a counter that
# only moves when a fake explicitly moves it. A sleep-based version of these
# assertions would be flaky under CI load and could not pin a residual at all.
#
# `_CLOCK` is a module global, so a fake counter is shared process-wide while a
# run fans out. Any fake-clock test driving more than one repo therefore pins
# `index_concurrency=1`, which gives one worker thread and one deterministic
# advance sequence.

# Anchored and exhaustive on purpose: this single constant pins the field set,
# the field ORDER, the `%.2f` shape, the mandatory single-space separator, and
# the absence of anything else on the line (no config values, no counts, no
# headers -- the redaction posture). `\d+\.\d\d` also rejects a negative value
# outright, which is what makes the `max(0.0, ...)` clamps in job.py testable.
_TIMING_RE = re.compile(
    r"^phase timing (?P<repo>[^ @]+)@(?P<branch>\S+): "
    r"total=(\d+\.\d\d)s resolve=(\d+\.\d\d)s download=(\d+\.\d\d)s "
    r"parse=(\d+\.\d\d)s embed=(\d+\.\d\d)s "
    r"db=(\d+\.\d\d)s sweep=(\d+\.\d\d)s other=(\d+\.\d\d)s$"
)


class _FakeClock:
    """A monotonic clock that advances only when a test advances it."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _install_fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Patch the one clock seam every asserted duration reads."""
    clock = _FakeClock()
    monkeypatch.setattr("indexer.timing._CLOCK", clock)
    return clock


def _timing_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [m for r in caplog.records if (m := r.getMessage()).startswith("phase timing ")]


def _phases(message: str) -> dict[str, float]:
    """Parse one `phase timing` line into ``{field: seconds}``, pinning its shape first."""
    assert _TIMING_RE.match(message), f"malformed timing line: {message!r}"
    body = message.split(": ", 1)[1]
    return {k: float(v.removesuffix("s")) for k, v in (f.split("=") for f in body.split(" "))}


def _only_phases(caplog: pytest.LogCaptureFixture) -> dict[str, float]:
    """The single expected timing line's fields."""
    messages = _timing_messages(caplog)
    assert len(messages) == 1, f"expected exactly one timing line, got {messages}"
    return _phases(messages[0])


@pytest.mark.unit
def test_indexed_branch_logs_every_phase_field(caplog: pytest.LogCaptureFixture) -> None:
    """T1: one record per indexed branch, with all eight fields in a fixed order."""
    idx = _RecordingIndex()
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), idx)
    assert code == 0

    messages = _timing_messages(caplog)
    assert len(messages) == 1
    match = _TIMING_RE.match(messages[0])
    assert match is not None, messages[0]
    assert match.group("repo") == "acme/widgets"
    assert match.group("branch") == "main"
    assert list(_phases(messages[0])) == [
        "total",
        "resolve",
        "download",
        "parse",
        "embed",
        "db",
        "sweep",
        "other",
    ]


@pytest.mark.unit
def test_semantic_off_still_logs_embed_zero(caplog: pytest.LogCaptureFixture) -> None:
    """T2 (AC2): the format string has no branches -- semantic off zeroes embed, it does
    not drop the field. A conditional line would break every grep an operator writes."""
    cfg_off = Settings(semantic_enabled=False)
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        assert _run(_config(repos=["acme/widgets"]), _RecordingIndex(), cfg=cfg_off) == 0
    off = _only_phases(caplog)
    assert off["embed"] == 0.0

    caplog.clear()
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            _RecordingIndex(),
            cfg=cfg,
            embed_fn=lambda texts: [[0.0] for _ in texts],
        )
    assert code == 0
    on = _only_phases(caplog)

    # Byte-identical field set and order, semantic on or off.
    assert list(on) == list(off)


@pytest.mark.unit
def test_semantic_on_attributes_embed_time(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3: chunking + embedding is its own phase, and it is NOT double-counted into db."""
    clock = _install_fake_clock(monkeypatch)

    def _embed(texts: list[str]) -> list[list[float]]:
        clock.advance(4.0)
        return [[0.0] for _ in texts]

    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), _RecordingIndex(), cfg=cfg, embed_fn=_embed)
    assert code == 0

    phases = _only_phases(caplog)
    assert phases["embed"] == 4.0
    assert phases["db"] == 0.0  # the embed happens BEFORE engine.connect(), not inside it
    assert phases["total"] == 4.0
    assert phases["other"] == 0.0


@pytest.mark.unit
def test_phase_timing_line_carries_the_repo_context(caplog: pytest.LogCaptureFixture) -> None:
    """T4 (AC1): emitted from inside the worker, so `[%(repo)s]` resolves.

    The drain loop's `indexed ...` line runs on the main thread, where the context
    var is at its default and the record would render `[-]` -- which is exactly
    why the timing line is NOT appended there.
    """
    import indexer.job as job

    log_filter = job.RepoLogFilter()
    caplog.handler.addFilter(log_filter)
    try:
        with caplog.at_level(logging.INFO, logger="indexer.job"):
            code = _run(_config(repos=["acme/widgets"]), _RecordingIndex())
    finally:
        caplog.handler.removeFilter(log_filter)
    assert code == 0

    timing_records = [r for r in caplog.records if r.getMessage().startswith("phase timing ")]
    assert len(timing_records) == 1
    assert timing_records[0].repo == "acme/widgets"


@pytest.mark.unit
def test_parse_time_is_excluded_from_db_time(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5: items are produced lazily INSIDE index_repo's transaction, so a naive
    "time index_fn" would report parse and db fused. _timed_items charges only the
    production of each item to parse, leaving the DML between them to db.

    Patches ``indexer.extract_pool.extract_file`` (#108 moved the in-process
    extraction call site there from ``indexer.job``) -- with the kill switch
    pinned by ``_config``'s default, ``ExtractionPool.stream()`` degrades to
    calling exactly that name per file, so this still measures the real
    ``_timed_items`` wrap around the pool's in-process path.
    """
    import indexer.extract_pool as extract_pool

    clock = _install_fake_clock(monkeypatch)
    real_extract_file = extract_pool.extract_file

    def _slow_extract(pf: ParsedFile) -> Any:
        clock.advance(2.0)
        return real_extract_file(pf)

    monkeypatch.setattr(extract_pool, "extract_file", _slow_extract)

    def _index(conn: Any, *, items: Any, **_: Any) -> IndexCounts:
        consumed = list(items)
        clock.advance(9.0)
        return IndexCounts(files=len(consumed), symbols=0, swept=0, edges=0)

    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), _index)
    assert code == 0

    phases = _only_phases(caplog)
    assert phases["parse"] == 4.0  # main.py + README.md, 2.0s each
    assert phases["db"] == 9.0  # NOT 13.0
    assert phases["total"] == 13.0
    assert phases["other"] == 0.0


@pytest.mark.unit
def test_skipped_branch_logs_no_timing_line(caplog: pytest.LogCaptureFixture) -> None:
    """T6: AC1 says "every indexed (not skipped) branch". A skipped branch has no
    phases to report and must not pollute the dominant-phase grep with zeroes."""
    engine = _FakeEngine(
        stamps={("acme/widgets", "main"): ("sha_widgets", INDEX_SEMANTICS_VERSION)}
    )
    idx = _RecordingIndex()
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), idx, engine=engine)
    assert code == 0
    assert idx.calls == []
    assert "skipped acme/widgets@main" in caplog.text
    assert _timing_messages(caplog) == []


@pytest.mark.unit
def test_failed_branch_logs_no_timing_line(caplog: pytest.LogCaptureFixture) -> None:
    """T7: partial timings for a branch that never finished would be misleading."""

    def _boom(conn: Any, *, items: Any, **_: Any) -> IndexCounts:
        raise RuntimeError("boom")

    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), _boom)
    assert code == 1
    assert "failed to index acme/widgets@main" in caplog.text
    assert _timing_messages(caplog) == []


@pytest.mark.unit
def test_conflicted_branch_logs_no_timing_line(caplog: pytest.LogCaptureFixture) -> None:
    """T20: the StaleIndexError path is classified separately from T7's generic
    failure, and it too rolled the branch back -- nothing was indexed, so nothing
    is reported."""

    def _stale(conn: Any, *, name: str, branch: str, items: Any, **_: Any) -> IndexCounts:
        raise StaleIndexError(f"repo_branches row for {name}@{branch} changed")

    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), _stale)
    assert code == 0  # a conflict self-heals and does not fail the run
    assert "index conflict for acme/widgets@main" in caplog.text
    assert _timing_messages(caplog) == []


@pytest.mark.unit
def test_repo_line_reports_resolve_and_branch_listing(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T8: the two REPO-scoped costs belong to no branch's total.

    Without `resolve=` the default branch's HEAD resolve would show as
    `resolve=0.00s` on its phase line and look like a bug; without `list=` a
    glob-configured monorepo's paginated branch listing would appear in no field
    on no line at all.

    Deliberately asserts NO relation between the `in ...s` elapsed and the
    parenthesised values: the former keeps its own real `time.monotonic()`
    reading, so under a fake clock it reads ~0.00s while the others do not.
    """
    import indexer.job as job

    clock = _install_fake_clock(monkeypatch)
    real_resolve_ref = job.resolve_ref
    real_list_branches = job.list_branches

    def _resolve_ref(*args: Any, **kwargs: Any) -> tuple[str, str]:
        clock.advance(5.0)
        return real_resolve_ref(*args, **kwargs)

    def _list_branches(*args: Any, **kwargs: Any) -> list[str]:
        clock.advance(3.0)
        return real_list_branches(*args, **kwargs)

    monkeypatch.setattr(job, "resolve_ref", _resolve_ref)
    monkeypatch.setattr(job, "list_branches", _list_branches)

    # No `branches:` globs -> the branches endpoint is never called at all.
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        assert _run(_config(repos=["acme/widgets"]), _RecordingIndex()) == 0
    finished = re.search(
        r"finished acme/widgets in ([0-9.]+)s \(resolve=([0-9.]+)s list=([0-9.]+)s\)", caplog.text
    )
    assert finished is not None, caplog.text
    assert float(finished.group(2)) == 5.0
    assert float(finished.group(3)) == 0.0
    # The pre-existing unanchored assertion on this line still matches.
    assert re.search(r"finished acme/widgets in ([0-9.]+)s", caplog.text) is not None

    caplog.clear()
    github = _GitHub(branches={"acme/widgets": ["main"]})
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"], branches=["*"], index_concurrency=1),
            _RecordingIndex(),
            github=github,
        )
    assert code == 0
    globbed = re.search(
        r"finished acme/widgets in ([0-9.]+)s \(resolve=([0-9.]+)s list=([0-9.]+)s\)", caplog.text
    )
    assert globbed is not None, caplog.text
    assert float(globbed.group(2)) == 5.0
    assert float(globbed.group(3)) == 3.0
    # ...and none of it leaked into the branch's own total.
    assert _only_phases(caplog)["total"] == 0.0


@pytest.mark.unit
def test_timings_do_not_leak_between_branches_of_one_repo(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T9: branch 2's numbers are its own, not cumulative.

    The trap this guards is reusing `_index_one_branch`'s `started` parameter for
    `total=`: that value is set once per REPO and passed unchanged to every
    branch, so branch 2 would report branch 1's wall clock too and silently
    inflate `other`. Also guards the ContextVar reset between branches.
    """
    import indexer.job as job

    clock = _install_fake_clock(monkeypatch)
    real_download = job.download_tarball

    def _download(*args: Any, **kwargs: Any) -> Any:
        clock.advance(5.0)
        return real_download(*args, **kwargs)

    monkeypatch.setattr(job, "download_tarball", _download)

    github = _GitHub(branches={"acme/widgets": ["main", "feature"]})
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"], branches=["feature"], index_concurrency=1),
            _RecordingIndex(),
            github=github,
        )
    assert code == 0

    messages = _timing_messages(caplog)
    assert len(messages) == 2
    branches = {_TIMING_RE.match(m).group("branch") for m in messages}  # type: ignore[union-attr]
    assert branches == {"main", "feature"}
    for message in messages:
        phases = _phases(message)
        assert phases["download"] == 5.0
        assert phases["total"] == 5.0  # not 10.0 on the second branch
        assert phases["other"] == 0.0


@pytest.mark.unit
def test_timings_do_not_leak_between_repos_on_a_reused_worker_thread(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T10: ThreadPoolExecutor reuses worker threads without resetting their
    context. With one worker and two repos, repo 2 runs on the same thread that
    just finished repo 1 -- a leaked timer would show up here as doubled numbers."""
    import indexer.job as job

    clock = _install_fake_clock(monkeypatch)
    real_download = job.download_tarball

    def _download(*args: Any, **kwargs: Any) -> Any:
        clock.advance(3.0)
        return real_download(*args, **kwargs)

    monkeypatch.setattr(job, "download_tarball", _download)

    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets", "acme/gadgets"], index_concurrency=1),
            _RecordingIndex(),
        )
    assert code == 0

    messages = _timing_messages(caplog)
    assert len(messages) == 2
    assert {_TIMING_RE.match(m).group("repo") for m in messages} == {  # type: ignore[union-attr]
        "acme/widgets",
        "acme/gadgets",
    }
    for message in messages:
        phases = _phases(message)
        assert phases["download"] == 3.0
        assert phases["total"] == 3.0
        assert phases["other"] == 0.0


@pytest.mark.unit
def test_timer_reset_fires_on_every_branch_exit_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Directly guards the `finally: reset_timer(token)` invariant itself.

    T9/T10 assert branch totals aren't cumulative, but `_index_one_branch`
    unconditionally does `install_timer(PhaseTimer())` at entry -- which
    shadows a prior leaked timer regardless of whether `reset_timer` actually
    ran. So T9/T10 pass identically whether or not the `finally` fires, and
    cannot detect its removal. This test spies on `indexer.timing.reset_timer`
    directly and asserts it is called exactly once per branch attempt,
    including the failed and conflicted paths -- the actual invariant the
    module docstring calls "mandatory".
    """
    import indexer.job as job
    import indexer.timing as timing

    calls = 0
    real_reset = timing.reset_timer

    def _spy_reset(token: Any) -> None:
        nonlocal calls
        calls += 1
        real_reset(token)

    monkeypatch.setattr(job, "reset_timer", _spy_reset)

    def _ok(conn: Any, *, items: Any, **_: Any) -> IndexCounts:
        return IndexCounts(files=len(list(items)), symbols=0, swept=0, edges=0)

    def _boom(conn: Any, *, items: Any, **_: Any) -> IndexCounts:
        raise RuntimeError("boom")

    def _stale(conn: Any, *, name: str, branch: str, items: Any, **_: Any) -> IndexCounts:
        raise StaleIndexError(f"repo_branches row for {name}@{branch} changed")

    _run(_config(repos=["acme/widgets"]), _ok)
    assert calls == 1

    _run(_config(repos=["acme/widgets"]), _boom)
    assert calls == 2

    _run(_config(repos=["acme/widgets"]), _stale)
    assert calls == 3


@pytest.mark.unit
def test_phase_timing_failure_does_not_fail_an_already_committed_branch(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug in the phase-timing arithmetic/log call must degrade to a missing
    log line, never to reclassifying an already-committed branch as `failed`.

    By the point this block runs, `index_fn` has already returned `counts` --
    this branch's transaction is committed. Flipping it to `failed` here would
    report a real committed index as a failure, flip the run's exit code, and
    gate off `_decide_reconciliation` (which requires zero failures), all for a
    bug in pure measurement code. Forces the failure at the `logger.info` call
    itself (the last statement in the guarded block) so a passing test proves
    the whole block is covered, not just the arithmetic above it.
    """
    import indexer.job as job

    real_info = job.logger.info

    def _boom_on_phase_timing(msg: object, *args: Any, **kwargs: Any) -> None:
        if isinstance(msg, str) and msg.startswith("phase timing "):
            raise RuntimeError("boom")
        real_info(msg, *args, **kwargs)

    monkeypatch.setattr(job.logger, "info", _boom_on_phase_timing)

    idx = _RecordingIndex()
    with caplog.at_level(logging.WARNING, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), idx)

    assert code == 0
    assert idx.calls == ["acme/widgets"]
    # No well-formed `phase timing ...: total=...` line was emitted (the raise
    # happened inside the logger.info call itself) -- but the module's own
    # "phase timing unavailable" fallback below shares the "phase timing "
    # prefix, so check the anchored data-line shape rather than the prefix.
    assert not any(_TIMING_RE.match(r.getMessage()) for r in caplog.records)
    assert "failed to index" not in caplog.text
    assert "phase timing unavailable for acme/widgets@main" in caplog.text


@pytest.mark.unit
def test_index_counts_fields_are_unchanged() -> None:
    """T11 (AC3): a tripwire that timing never leaks into the frozen return type.

    `IndexCounts` is compared by value in existing assertions across the unit and
    integration suites; a timing field would be nonzero by construction and break
    every one of them. The sweep duration reaches job.py ambiently for exactly
    this reason.
    """
    assert [f.name for f in dataclasses.fields(IndexCounts)] == [
        "files",
        "symbols",
        "swept",
        "edges",
    ]


@pytest.mark.unit
def test_timing_line_contains_no_values_beyond_durations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T12 (AC3 / redaction): the anchored regex matches the WHOLE message, so
    nothing but the repo, the branch, and the nine durations can ever appear --
    no config values, no counts, no headers -- and no field can go negative."""
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    config = _config(repos=["acme/widgets"], semantic_max_chunks_per_repo={"acme/widgets": 7})
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            config,
            _RecordingIndex(),
            cfg=cfg,
            embed_fn=lambda texts: [[0.0] for _ in texts],
        )
    assert code == 0

    messages = _timing_messages(caplog)
    assert len(messages) == 1
    assert _TIMING_RE.fullmatch(messages[0]) is not None, messages[0]


@pytest.mark.unit
def test_sweep_time_is_subtracted_from_db_time(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T16: the cross-module sweep hook, pinned without Postgres.

    A fake `index_fn` runs on the worker thread inside the installed timer's
    context, so its `record("sweep", ...)` reaches exactly the timer the real
    `store.py` hook would. Without this test the subtraction term is zero in
    every unit test and the arithmetic is unproven under `make test`.
    """
    import indexer.timing as timing

    clock = _install_fake_clock(monkeypatch)

    def _index(conn: Any, *, items: Any, **_: Any) -> IndexCounts:
        consumed = list(items)
        clock.advance(2.0)  # the sweep really burned this time...
        timing.record("sweep", 2.0)  # ...and store.py reports it ambiently
        clock.advance(6.0)  # the rest of the transaction
        return IndexCounts(files=len(consumed), symbols=0, swept=1, edges=0)

    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), _index)
    assert code == 0

    phases = _only_phases(caplog)
    assert phases["sweep"] == 2.0
    assert phases["db"] == 6.0  # NOT 8.0
    assert phases["total"] == 8.0
    assert phases["other"] == 0.0


@pytest.mark.unit
def test_semantic_eager_walk_is_charged_to_parse(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T18: on the semantic (production) path the file list is materialized up front,
    OUTSIDE the db window and outside _timed_items. Leaving that walk unwrapped would
    dump the whole stream + decompress + decode cost into `other` -- the exact number
    that routes work between the epic's parse/IO/db issues."""
    import indexer.job as job

    clock = _install_fake_clock(monkeypatch)
    real_iter_tar_source_files = job.iter_tar_source_files

    def _slow_walk(tar_path: Any) -> Any:
        # iter_tar_source_files is a generator function: paying the cost at call
        # time (before the first `next()`) would let this fake pass even if
        # the real wrap only timed the call and not the iteration -- the
        # exact mis-scoped-wrap bug this test exists to catch. Pay per
        # yielded file instead, matching where the real cost (tar stream,
        # decompress, decode) is actually incurred.
        for pf in real_iter_tar_source_files(tar_path):
            clock.advance(3.0)  # 2 files in _DEFAULT_FILES -> 6.0s total
            yield pf

    monkeypatch.setattr(job, "iter_tar_source_files", _slow_walk)

    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]),
            _RecordingIndex(),
            cfg=cfg,
            embed_fn=lambda texts: [[0.0] for _ in texts],
        )
    assert code == 0

    phases = _only_phases(caplog)
    assert phases["parse"] == 6.0
    assert phases["other"] == 0.0  # not swallowed by the residual
    assert phases["db"] == 0.0  # and not misattributed to the transaction
    assert phases["total"] == 6.0


@pytest.mark.unit
def test_embed_degrade_path_still_logs_a_timing_line(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T19: a downed embedder degrades to a core index -- and it burned real time
    getting there, so `embed=` must still report it. That is why the embed wrap is
    the one phase recorded in a `finally`."""
    clock = _install_fake_clock(monkeypatch)

    def _down(_texts: list[str]) -> list[list[float]]:
        clock.advance(7.0)
        raise RuntimeError("serving endpoint unavailable")

    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets"]), idx, cfg=cfg, embed_fn=_down)
    assert code == 0
    assert idx.calls == ["acme/widgets"]  # the core index still ran
    assert idx.chunk_writer is None  # ...without chunks

    phases = _only_phases(caplog)
    assert phases["embed"] == 7.0
    assert phases["total"] == 7.0
    assert phases["other"] == 0.0


@pytest.mark.unit
def test_other_is_the_exact_unattributed_residual(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T17: pins the RESIDUAL, not the sum identity.

    `other` is *defined* as `total - sum(phases)`, so asserting
    `total == sum(phases) + other` is a tautology that holds for any values and can
    never detect a mis-measured phase. This test instead drives a distinct known
    amount through every one of the six phases and requires `other == 0.00s`,
    then re-runs with one deliberately UNINSTRUMENTED advance and requires `other`
    to equal exactly that amount.
    """
    import indexer.job as job

    clock = _install_fake_clock(monkeypatch)
    real_resolve_branch_head = job.resolve_branch_head
    real_download_tarball = job.download_tarball
    real_iter_tar_source_files = job.iter_tar_source_files
    real_assert_disk_headroom = job.assert_disk_headroom

    def _resolve_branch_head(*args: Any, **kwargs: Any) -> str:
        clock.advance(1.0)
        return real_resolve_branch_head(*args, **kwargs)

    def _download_tarball(*args: Any, **kwargs: Any) -> Any:
        clock.advance(2.0)
        return real_download_tarball(*args, **kwargs)

    def _iter_tar_source_files(tar_path: Any) -> Any:
        # Pay per yielded file, not at call time -- iter_tar_source_files is a
        # generator function, so a call-time advance would pass even for a
        # wrap that only times the call and not the iteration.
        for pf in real_iter_tar_source_files(tar_path):
            clock.advance(4.0)  # 2 files in _DEFAULT_FILES -> 8.0s total
            yield pf

    def _embed(texts: list[str]) -> list[list[float]]:
        clock.advance(16.0)
        return [[0.0] for _ in texts]

    def _index(conn: Any, *, items: Any, **_: Any) -> IndexCounts:
        import indexer.timing as timing

        consumed = list(items)
        clock.advance(32.0)  # the transaction's own DML
        clock.advance(64.0)  # the sweep, which store.py reports ambiently
        timing.record("sweep", 64.0)
        return IndexCounts(files=len(consumed), symbols=0, swept=0, edges=0)

    monkeypatch.setattr(job, "resolve_branch_head", _resolve_branch_head)
    monkeypatch.setattr(job, "download_tarball", _download_tarball)
    monkeypatch.setattr(job, "iter_tar_source_files", _iter_tar_source_files)

    cfg = Settings(semantic_enabled=True, semantic_max_chunks_per_repo=100)
    # "main" is stamped current so it is SKIPPED, leaving exactly one indexed
    # branch -- and a non-default one, which is the only way `resolve=` is
    # non-zero (the default branch's HEAD came from the repo-level resolve_ref).
    engine = _FakeEngine(
        stamps={("acme/widgets", "main"): ("sha_widgets", INDEX_SEMANTICS_VERSION)}
    )
    github = _GitHub(branches={"acme/widgets": ["main", "feature"]})
    config = _config(repos=["acme/widgets"], branches=["feature"], index_concurrency=1)

    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(config, _index, cfg=cfg, embed_fn=_embed, github=github, engine=engine)
    assert code == 0

    phases = _only_phases(caplog)
    assert phases == {
        "total": 123.0,
        "resolve": 1.0,
        "download": 2.0,
        "parse": 8.0,
        "embed": 16.0,
        "db": 32.0,
        "sweep": 64.0,
        "other": 0.0,
    }

    # Second leg: advance the clock at a point NOTHING instruments (the pre-flight
    # disk check). Every phase must be unchanged and `other` must be exactly it.
    def _assert_disk_headroom(*args: Any, **kwargs: Any) -> None:
        clock.advance(9.0)
        real_assert_disk_headroom(*args, **kwargs)

    monkeypatch.setattr(job, "assert_disk_headroom", _assert_disk_headroom)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(config, _index, cfg=cfg, embed_fn=_embed, github=github, engine=engine)
    assert code == 0

    phases = _only_phases(caplog)
    assert phases == {
        "total": 132.0,
        "resolve": 1.0,
        "download": 2.0,
        "parse": 8.0,
        "embed": 16.0,
        "db": 32.0,
        "sweep": 64.0,
        "other": 9.0,
    }


# --- process-pool extraction (#108) -------------------------------------------


@pytest.mark.unit
def test_default_helper_config_creates_no_extraction_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T9: the shared _config()/_run() helper pair pins extract_processes=1, so
    the ~150 run() tests in this file spawn no worker processes and run no
    preflight probe. Without this pin every one of them would spawn up to 8
    workers, each re-importing tree_sitter_language_pack -- minutes slower and
    flaky under CI load."""
    probe_calls = {"n": 0}

    def _tracking_probe(*args: Any, **kwargs: Any) -> bool:
        probe_calls["n"] += 1
        return True

    monkeypatch.setattr("indexer.extract_pool._run_preflight_probe", _tracking_probe)

    code = _run(_config(repos=["acme/widgets"]), _RecordingIndex())
    assert code == 0
    assert probe_calls["n"] == 0


@pytest.mark.unit
def test_pool_engaged_attributes_stream_production_to_parse(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T10: phase-timing attribution survives the pool. The pool is built in
    run(), OUTSIDE branch_started, so it contributes nothing to any branch's
    total by itself; only the per-item production _timed_items wraps is
    charged, and it still lands on `parse`, never `other`."""
    clock = _install_fake_clock(monkeypatch)

    class _ClockAdvancingExecutor:
        """Advances the fake clock once per submitted batch, proportional to
        its file count -- mirrors this file's other `_slow_*` timing fakes,
        just at the pool's own submit() seam instead of a direct function
        patch."""

        def submit(self, fn: Any, *args: Any) -> "Future[list[FileExtraction]]":
            batch = args[0]
            clock.advance(1.5 * len(batch))
            fut: "Future[list[FileExtraction]]" = Future()
            fut.set_result(fn(*args))
            return fut

    pool = ExtractionPool(n_processes=0, batch_bytes=10_000_000)  # one big batch
    pool._n_processes = 2
    pool._executor = _ClockAdvancingExecutor()  # type: ignore[assignment]

    idx = _RecordingIndex()
    cfg = Settings(semantic_enabled=False)
    # Both files must be a recognized language (unlike _DEFAULT_FILES' README.md,
    # `lang=None`) so BOTH qualify for the pool and land in the SAME one-file-
    # exceeds-nothing batch -- otherwise D5's local short-circuit answers one of
    # them without ever reaching the executor, undercounting this assertion.
    github = _GitHub(
        files={"a.py": b"def f():\n    return 1\n", "b.py": b"def g():\n    return 2\n"}
    )
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(
            _config(repos=["acme/widgets"]), idx, cfg=cfg, github=github, extraction_pool=pool
        )
    assert code == 0

    phases = _only_phases(caplog)
    assert list(phases) == [
        "total",
        "resolve",
        "download",
        "parse",
        "embed",
        "db",
        "sweep",
        "other",
    ]
    assert phases["parse"] == 3.0  # a.py + b.py (the github= override above) x 1.5s, one batch
    assert phases["other"] == 0.0
    assert phases["db"] == 0.0
    assert phases["total"] == 3.0


@pytest.mark.unit
def test_extraction_pool_is_shut_down_even_when_fan_out_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool lifecycle: built once per run, shut down in the `finally` even when
    something inside the fan-out block raises before any branch runs."""
    import indexer.job as job

    shutdown_calls = {"n": 0}

    class _FakePool:
        def shutdown(self) -> None:
            shutdown_calls["n"] += 1

    monkeypatch.setattr(job, "build_extraction_pool", lambda config: _FakePool())

    def _boom_stamps(engine: Any, entries: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(job, "_read_stamps", _boom_stamps)

    with pytest.raises(RuntimeError):
        _run(_config(repos=["acme/widgets"]), _RecordingIndex())

    assert shutdown_calls["n"] == 1


@pytest.mark.unit
def test_pool_is_built_before_the_first_db_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """R2 (§10.3 of the plan): the preflight probe is the first process spawn in
    run(), and it MUST precede any database read or write. A re-entrant
    python_wheel_task child under an unguarded __main__ inherits the parent's
    sys.argv and would otherwise become a second corpus writer; the probe's
    unguarded-__main__ crash (reproduced: RuntimeError from
    multiprocessing.spawn._check_not_importing_main) only bounds that risk if
    it runs before the child could touch the database. This pins the ordering
    directly so a future refactor (e.g. "only build the pool if a branch needs
    it") cannot silently move the pool build after _read_stamps without a test
    failing -- do not "fix" a failure here by reordering the assertion."""
    import indexer.job as job

    calls: list[str] = []

    def _tracking_build(config: Any) -> Any:
        calls.append("pool")
        return ExtractionPool(n_processes=0)

    real_read_stamps = job._read_stamps

    def _tracking_read_stamps(*args: Any, **kwargs: Any) -> Any:
        calls.append("stamps")
        return real_read_stamps(*args, **kwargs)

    monkeypatch.setattr(job, "build_extraction_pool", _tracking_build)
    monkeypatch.setattr(job, "_read_stamps", _tracking_read_stamps)

    code = _run(_config(repos=["acme/widgets"]), _RecordingIndex())
    assert code == 0
    assert calls == ["pool", "stamps"]


@pytest.mark.unit
def test_injected_extraction_pool_is_never_shut_down_by_run() -> None:
    """Mirrors owns_engine/owns_http: a CALLER-supplied pool is the caller's to
    manage, never torn down inside run()."""
    shutdown_calls = {"n": 0}

    class _FakePool:
        def stream(self, files: Any) -> Any:
            return ((pf, FileExtraction(symbols=[], edges=[])) for pf in files)

        def shutdown(self) -> None:
            shutdown_calls["n"] += 1

    code = _run(_config(repos=["acme/widgets"]), _RecordingIndex(), extraction_pool=_FakePool())
    assert code == 0
    assert shutdown_calls["n"] == 0


@pytest.mark.unit
def test_broken_pool_fails_only_the_in_flight_branch_next_branch_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC3: a shared pool's break fails the branch it broke under; the run
    continues and the NEXT branch of the same repo indexes normally -- the
    per-branch classification this design maps ExtractionPoolError onto."""
    call_count = {"n": 0}

    class _FlakyPool:
        def stream(self, files: Any) -> Any:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ExtractionPoolError("simulated pool break (generation 0)")
            return ((pf, FileExtraction(symbols=[], edges=[])) for pf in files)

        def shutdown(self) -> None:
            pass

    idx = _RecordingIndex()
    github = _GitHub(branches={"acme/widgets": ["main", "feature"]})
    # Default branch ("main") is always first and always included -- see
    # indexer.branches.resolve_branches -- so "main" is the branch that breaks
    # and "feature" is the one that recovers.
    config = _config(repos=["acme/widgets"], branches=["feature"], index_concurrency=1)

    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(config, idx, github=github, extraction_pool=_FlakyPool())

    assert code == 1  # the run continues but is not clean
    assert "failed to index acme/widgets@main" in caplog.text
    assert idx.calls == ["acme/widgets"]  # only "feature" reached index_repo
