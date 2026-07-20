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
import re
import subprocess
import sys
import tarfile
import threading
import time
from typing import Any, NamedTuple

import httpx
import pytest

from app.config import Settings
from app.db.models import INDEX_SEMANTICS_VERSION
from indexer.job import read_github_token, run
from indexer.languages import ExtractedSymbol, IndexCounts, ParsedFile
from indexer.repo_config import ConfigError, RepoConfig, load_config
from indexer.store import StaleIndexError

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
    ) -> None:
        self.enumerations = enumerations or {}
        self.missing = missing or set()
        self.files = files
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
                return httpx.Response(200, json={"sha": f"sha_{repo}"})
            if parts[3] == "tarball":
                return httpx.Response(200, content=_tarball(f"{org}-{repo}-shashas", self.files))
        return httpx.Response(404)

    @property
    def tarball_requests(self) -> list[str]:
        return [p for p in self.paths if "/tarball" in p]


def _repo_meta(full_name: str, **overrides: Any) -> dict[str, Any]:
    """One GitHub list-repos object, defaulting to a repo nothing excludes."""
    return {"full_name": full_name, "fork": False, "archived": False, "size": 10, **overrides}


def _config(*, index_concurrency: int | None = None, **connection: Any) -> RepoConfig:
    """A single-github-connection RepoConfig from selector kwargs."""
    doc: dict[str, Any] = {"version": 1, "connections": [{"type": "github", **connection}]}
    if index_concurrency is not None:
        doc["index_concurrency"] = index_concurrency
    return RepoConfig.model_validate(doc)


class _StampRow(NamedTuple):
    """One row of the pre-fan-out stamp SELECT."""

    name: str
    last_indexed_commit: str | None
    index_semantics_version: int | None


class _FakeResult:
    def __init__(self, rows: list[_StampRow]) -> None:
        self._rows = rows

    def all(self) -> list[_StampRow]:
        return self._rows


class _FakeConn:
    def __init__(self, engine: _FakeEngine | None = None) -> None:
        self._engine = engine

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, stmt: Any) -> _FakeResult:
        assert self._engine is not None
        self._engine.executed.append(stmt)
        return _FakeResult(self._engine.stamp_rows)


class _FakeEngine:
    """Fake Engine that also answers ``_read_stamps``' batched SELECT.

    ``stamps`` maps a repo name to its stored
    ``(last_indexed_commit, index_semantics_version)``; an absent repo is simply
    not returned, which is the "never indexed" shape.
    """

    def __init__(self, stamps: dict[str, tuple[str | None, int | None]] | None = None) -> None:
        self.disposed = False
        self.disposed_at: float | None = None
        self.executed: list[Any] = []
        self.stamp_rows = [
            _StampRow(name, commit, version) for name, (commit, version) in (stamps or {}).items()
        ]

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
    engine: _FakeEngine | None = None,
) -> int:
    """Drive run() with a faked config read but a REAL resolve_repos."""
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
    engine = _FakeEngine(stamps={"acme/widgets": ("sha_widgets", INDEX_SEMANTICS_VERSION)})
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
    engine = _FakeEngine(stamps={"acme/widgets": ("sha_widgets", INDEX_SEMANTICS_VERSION - 1)})
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
    engine = _FakeEngine(stamps={"acme/widgets": ("sha_widgets", None)})
    code = _run(_config(repos=["acme/widgets"]), idx, engine=engine)
    assert code == 0
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_run_reindexes_when_head_has_moved() -> None:
    idx = _RecordingIndex()
    engine = _FakeEngine(stamps={"acme/widgets": ("sha_old", INDEX_SEMANTICS_VERSION)})
    code = _run(_config(repos=["acme/widgets"]), idx, engine=engine)
    assert code == 0
    assert idx.calls == ["acme/widgets"]


@pytest.mark.unit
def test_run_indexes_a_repo_with_no_stamp_at_all() -> None:
    """An unknown repo gets stamp (None, None), which can never equal HEAD."""
    idx = _RecordingIndex()
    engine = _FakeEngine(stamps={"other/thing": ("sha_widgets", INDEX_SEMANTICS_VERSION)})
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
    assert len(engine.executed) == 1
    sql = str(engine.executed[0])
    assert "repos.name IN " in sql
    assert "repos.last_indexed_commit" in sql
    assert "repos.index_semantics_version" in sql


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
        return IndexCounts(files=0, symbols=0, swept=0)


@pytest.mark.unit
def test_two_repos_are_indexed_concurrently_at_concurrency_two() -> None:
    """Concurrency proof: both workers must be inside index_fn simultaneously.

    NOTE this proves CONCURRENCY, not throughput. It would pass identically in a
    world where fan-out delivers zero speedup — symbol extraction itself measured
    0.95x on 4 threads. Throughput is measured on the first production run, via
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
    the stamp (realistically an operator running the force-reindex mid-run) makes
    the next run re-index this repo unconditionally. That is the rationale, NOT
    that the work was redundant and NOT that the index is already correct: for
    one run, that repo is stale. So: its own bucket, WARNING with no traceback,
    excluded from the exit code, and a message that says so plainly.
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
        return IndexCounts(files=1, symbols=0, swept=0)

    engine = _FakeEngine(stamps={"acme/skipped": ("sha_skipped", INDEX_SEMANTICS_VERSION)})
    github = _GitHub(missing={"acme/broken"})
    entries = ["acme/widgets", "acme/skipped", "acme/conflicted", "acme/broken"]
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=entries), _mixed, github=github, engine=engine)
    assert code == 1  # the failure, not the conflict, is what fails the run

    completion = [r for r in caplog.records if "indexing complete" in r.getMessage()]
    assert len(completion) == 1
    message = completion[0].getMessage()
    assert "1 ok, 1 skipped, 1 conflicts, 1 failed (of 4)" in message


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
        return IndexCounts(files=0, symbols=0, swept=0)

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
    """AC1b's instrument. Without these lines, "throughput is measured on the
    first production run" is an unfalsifiable promise, so the numbers are
    asserted to be present and parseable — not merely assumed."""
    idx = _RecordingIndex()
    engine = _FakeEngine(stamps={"acme/gadgets": ("sha_gadgets", INDEX_SEMANTICS_VERSION)})
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        code = _run(_config(repos=["acme/widgets", "acme/gadgets"]), idx, engine=engine)
    assert code == 0

    indexed = _elapsed(caplog, r"finished acme/widgets in ([0-9.]+)s")
    skipped = _elapsed(caplog, r"skipped acme/gadgets: .* in ([0-9.]+)s")
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
# so records from indexer.fetch / indexer.store / indexer.embed — which carry no
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
        return IndexCounts(files=0, symbols=0, swept=0)

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

    assert _repo_ctx.get() == "-"
    with (
        httpx.Client(transport=httpx.MockTransport(_GitHub())) as client,
        pytest.raises(ValueError),
    ):
        # A malformed entry: normalize_repo raises AFTER the context is set.
        _index_one(
            "https://evil.com/a/b",
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_RecordingIndex(),
            cfg=Settings(semantic_enabled=False),
            embed_fn=None,
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
        for pf, syms in items:
            for sym in syms:
                assert isinstance(sym, ExtractedSymbol)
                collected.append((name, pf.path, sym.name, sym.kind, sym.start_line, sym.end_line))
        self.rows.extend(collected)
        return IndexCounts(files=0, symbols=len(collected), swept=0)

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

    With semantic on, effective_workers clamps 6 -> 2; a pool of 6 would then
    over-provision Lakebase connections that no worker can ever use. The clamp
    is also logged, because a run silently doing a third of the requested
    concurrency is otherwise invisible.
    """
    with caplog.at_level(logging.INFO, logger="indexer.job"):
        kwargs = _engine_kwargs(
            _config(repos=["acme/widgets"], index_concurrency=6),
            monkeypatch,
            cfg=Settings(semantic_enabled=True),
        )
    assert kwargs["pool_size"] == 2
    assert kwargs["max_overflow"] == 0
    assert "clamping index_concurrency 6 -> 2" in caplog.text


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
        if repo == "acme/gadgets":
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
