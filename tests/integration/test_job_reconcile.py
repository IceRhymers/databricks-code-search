"""Integration tests for indexer.job.run()'s clean-run reconciliation checkpoint (#59),
against a real local Postgres/Lakebase.

Final phase of the corpus-reconciliation epic (#56): unlike test_reconcile.py (Phase
1/2, storage primitives exercised directly), these drive the REAL run() end to end --
REAL reconcile_retired_branches/reconcile_removed_repos, REAL index_repo, a real
engine -- with only GitHub HTTP faked via httpx.MockTransport.

Fixture note: run() opens its OWN connections (one per worker, one at the
reconciliation checkpoint) rather than receiving a single held-open Connection, so
test_reconcile.py's style (one Connection with search_path SET on it) cannot make the
throwaway schema visible everywhere. This follows test_service.py's / test_webui_
semantic.py's PGOPTIONS idiom instead: the schema is baked into every pooled
connection by setting PGOPTIONS *before* the job's Engine is constructed.
"""

from __future__ import annotations

import base64
import io
import os
import tarfile
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.config import Settings
from app.db.client import create_db_engine
from app.db.models import Base
from indexer.job import run
from indexer.repo_config import RepoConfig

SCHEMA = "test_job_reconcile"

# Every scenario here runs with semantic search off: reconciliation (#59) is a
# core-corpus concern, chunks/embeddings are an orthogonal, already-covered layer
# (issue #14), and disabling it means no embedder/SDK is ever touched here, and no
# ``chunks`` table (nor the Lakebase-only ``lakebase_vector`` extension its column
# type needs -- absent on a plain local Postgres) has to exist at all.
_CFG = Settings(semantic_enabled=False)


@pytest.fixture
def engine() -> Iterator[Any]:
    admin_engine = create_db_engine()
    try:
        with admin_engine.connect() as admin_conn:
            admin_conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
            admin_conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
            admin_conn.commit()
            admin_conn.execute(text(f"SET search_path TO {SCHEMA}, public"))
            Base.metadata.create_all(bind=admin_conn)
            admin_conn.commit()
    finally:
        admin_engine.dispose()

    prev_pgoptions = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = f"-c search_path={SCHEMA},public"
    job_engine = create_db_engine(pool_size=4, max_overflow=0)
    try:
        yield job_engine
    finally:
        job_engine.dispose()
        if prev_pgoptions is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = prev_pgoptions
        cleanup_engine = create_db_engine()
        try:
            with cleanup_engine.connect() as c:
                c.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
                c.commit()
        finally:
            cleanup_engine.dispose()


# --- Verification helpers (open their own connection -- run() has already returned) ---


def _repo_names(engine: Any) -> set[str]:
    with engine.connect() as c:
        return set(c.execute(text("SELECT name FROM repos")).scalars().all())


def _repo_branch_names(engine: Any, name: str) -> set[str]:
    with engine.connect() as c:
        rows = (
            c.execute(
                text(
                    "SELECT rb.branch FROM repo_branches rb "
                    "JOIN repos r ON r.id = rb.repo_id WHERE r.name = :name"
                ),
                {"name": name},
            )
            .scalars()
            .all()
        )
    return set(rows)


def _branches_of(engine: Any, path: str, repo: str) -> list[str]:
    with engine.connect() as c:
        return list(
            c.execute(
                text(
                    "SELECT f.branches FROM files f JOIN repos r ON r.id = f.repo_id "
                    "WHERE f.path = :p AND r.name = :repo"
                ),
                {"p": path, "repo": repo},
            ).scalar_one()
        )


def _count(engine: Any, table: str, where: str = "") -> int:
    sql = f"SELECT count(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    with engine.connect() as c:
        return int(c.execute(text(sql)).scalar_one())


# --- GitHub fake ---------------------------------------------------------------

_DEFAULT_FILES = {"main.py": b"def f():\n    return 1\n"}


def _tarball(top_dir: str, files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, data in files.items():
            name = f"{top_dir}/{rel}"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _GitHub:
    """Recording GitHub fake. Every scenario below uses explicit ``repos:`` (never
    enumeration), so only repo metadata / commits / branches / tarball are answered.

    ``files`` is keyed ``(repo, branch)`` -- not by SHA -- so a test can give two
    branches of the same repo genuinely different (or IDENTICAL, for the
    shared-content scenario) file content. The tarball fetch recovers the branch
    name from ``sha_{repo}_{branch}`` (this fake's own SHA scheme) to look it up.
    """

    def __init__(
        self,
        *,
        default_branches: dict[str, str] | None = None,
        branches: dict[str, list[str]] | None = None,
        missing: set[str] | None = None,
        branches_fail: set[str] | None = None,
        files: dict[tuple[str, str], dict[str, bytes]] | None = None,
    ) -> None:
        self.default_branches = default_branches or {}
        self.branches = branches or {}
        self.missing = missing or set()
        self.branches_fail = branches_fail or set()
        self.files = files or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if len(parts) >= 3 and parts[0] == "repos":
            org, repo = parts[1], parts[2]
            full = f"{org}/{repo}"
            if len(parts) == 3:
                if full in self.missing:
                    return httpx.Response(404)
                return httpx.Response(
                    200, json={"default_branch": self.default_branches.get(full, "main")}
                )
            if parts[3] == "commits":
                ref = parts[4] if len(parts) > 4 else "main"
                return httpx.Response(200, json={"sha": f"sha_{repo}_{ref}"})
            if parts[3] == "branches":
                if full in self.branches_fail:
                    return httpx.Response(500)
                names = self.branches.get(full, [])
                return httpx.Response(200, json=[{"name": n} for n in names])
            if parts[3] == "tarball":
                ref = parts[4]
                branch = ref[len(f"sha_{repo}_") :]
                content = self.files.get((full, branch), _DEFAULT_FILES)
                return httpx.Response(200, content=_tarball(f"{org}-{repo}-x", content))
        return httpx.Response(404)


class _FakeSecret:
    value = base64.b64encode(b"tok").decode()


class _FakeSecrets:
    def get_secret(self, scope: str, key: str) -> _FakeSecret:
        return _FakeSecret()


class _FakeWorkspaceClient:
    secrets = _FakeSecrets()


def _config(*, repos: list[str], branches: list[str] | None = None) -> RepoConfig:
    connection: dict[str, Any] = {"type": "github", "repos": repos}
    if branches is not None:
        connection["branches"] = branches
    return RepoConfig.model_validate({"version": 1, "connections": [connection]})


def _run_job(engine: Any, config: RepoConfig, github: _GitHub) -> int:
    """Drive the REAL run() -- REAL index_repo, REAL reconcile_retired_branches /
    reconcile_removed_repos -- against ``engine``, with only GitHub HTTP faked."""
    with httpx.Client(transport=httpx.MockTransport(github)) as client:
        return run(
            config_path="/Workspace/x/config.yaml",
            scope="s",
            key="k",
            endpoint=None,
            database=None,
            workspace_client=_FakeWorkspaceClient(),
            http_client=client,
            engine=engine,
            cfg=_CFG,
            config_loader=lambda _c, _p: config,
        )


# --- Scenarios -------------------------------------------------------------


@pytest.mark.integration
def test_removed_repo_is_purged_on_next_clean_run(engine: Any) -> None:
    github = _GitHub()
    code = _run_job(engine, _config(repos=["acme/kept", "acme/removed"]), github)
    assert code == 0
    assert _repo_names(engine) == {"acme/kept", "acme/removed"}

    code = _run_job(engine, _config(repos=["acme/kept"]), github)
    assert code == 0
    assert _repo_names(engine) == {"acme/kept"}
    assert _count(engine, "repos", "name = 'acme/removed'") == 0


@pytest.mark.integration
def test_narrowed_branch_glob_retires_the_dropped_branch(engine: Any) -> None:
    github = _GitHub(branches={"acme/widgets": ["main", "feature-x"]})
    code = _run_job(engine, _config(repos=["acme/widgets"], branches=["*"]), github)
    assert code == 0
    assert _repo_branch_names(engine, "acme/widgets") == {"main", "feature-x"}

    # feature-x still exists upstream -- only the config's glob narrows.
    code = _run_job(engine, _config(repos=["acme/widgets"], branches=["main"]), github)
    assert code == 0
    assert _repo_branch_names(engine, "acme/widgets") == {"main"}


@pytest.mark.integration
def test_default_branch_flip_retires_the_old_default(engine: Any) -> None:
    github = _GitHub(default_branches={"acme/widgets": "main"})
    code = _run_job(engine, _config(repos=["acme/widgets"]), github)
    assert code == 0
    assert _repo_branch_names(engine, "acme/widgets") == {"main"}

    # Upstream default branch flips to 'trunk'; 'main' is deleted entirely.
    github.default_branches["acme/widgets"] = "trunk"
    code = _run_job(engine, _config(repos=["acme/widgets"]), github)
    assert code == 0
    assert _repo_branch_names(engine, "acme/widgets") == {"trunk"}


@pytest.mark.integration
def test_shared_content_survives_when_the_other_branch_is_retired(engine: Any) -> None:
    shared = {"shared.py": b"def f():\n    return 1\n"}
    only_feature = {"shared.py": b"def f():\n    return 1\n", "extra.py": b"x = 1\n"}
    github = _GitHub(
        branches={"acme/widgets": ["main", "feature"]},
        files={("acme/widgets", "main"): shared, ("acme/widgets", "feature"): only_feature},
    )
    code = _run_job(engine, _config(repos=["acme/widgets"], branches=["*"]), github)
    assert code == 0
    assert sorted(_branches_of(engine, "shared.py", "acme/widgets")) == ["feature", "main"]

    # Narrow away 'feature' -- shared.py's row must SURVIVE (main still uses it),
    # only losing 'feature' from its membership array.
    code = _run_job(engine, _config(repos=["acme/widgets"]), github)
    assert code == 0
    assert _branches_of(engine, "shared.py", "acme/widgets") == ["main"]
    assert _count(engine, "files", "path = 'extra.py'") == 0  # feature-only file is gone


@pytest.mark.integration
def test_partial_failure_leaves_nothing_reconciled(engine: Any) -> None:
    github = _GitHub(branches={"acme/a": ["main", "stale"]})
    code = _run_job(engine, _config(repos=["acme/a"], branches=["*"]), github)
    assert code == 0
    assert _repo_branch_names(engine, "acme/a") == {"main", "stale"}

    # Second run: acme/a would cleanly retire 'stale' (narrowed glob), but
    # acme/b's branch listing 500s -- a repo-level failure elsewhere in the SAME
    # run must block reconciliation corpus-wide, not just for acme/b.
    github2 = _GitHub(branches_fail={"acme/b"})
    code = _run_job(engine, _config(repos=["acme/a", "acme/b"], branches=["*"]), github2)
    assert code == 1

    assert _repo_branch_names(engine, "acme/a") == {"main", "stale"}  # untouched


@pytest.mark.integration
def test_repo_failure_blocks_reconciliation_corpus_wide(engine: Any) -> None:
    github = _GitHub(branches={"acme/a": ["main", "stale"]})
    # Seed acme/a (with a branch that will become stale) and acme/c, which will
    # be entirely absent from every later run's config -- a purge candidate.
    code = _run_job(engine, _config(repos=["acme/a", "acme/c"], branches=["*"]), github)
    assert code == 0
    assert _repo_names(engine) == {"acme/a", "acme/c"}
    assert _repo_branch_names(engine, "acme/a") == {"main", "stale"}

    github2 = _GitHub(missing={"acme/b"})
    code = _run_job(engine, _config(repos=["acme/a", "acme/b"]), github2)
    assert code == 1  # acme/b's repo-level failure fails the run

    # Nothing reconciled ANYWHERE: acme/a's stale branch survives...
    assert _repo_branch_names(engine, "acme/a") == {"main", "stale"}
    # ...and acme/c (entirely absent from this run's config) also survives.
    assert "acme/c" in _repo_names(engine)


@pytest.mark.integration
def test_enumeration_shrink_withholds_purge_but_retires_branches_on_survivors(
    engine: Any,
) -> None:
    github = _GitHub(branches={"acme/a": ["main", "stale"]})
    code = _run_job(
        engine, _config(repos=["acme/a", "acme/b", "acme/c", "acme/d"], branches=["*"]), github
    )
    assert code == 0
    assert _repo_names(engine) == {"acme/a", "acme/b", "acme/c", "acme/d"}
    assert _repo_branch_names(engine, "acme/a") == {"main", "stale"}

    # Config narrows to keep only acme/a: would purge 3/4 stored repos -- a
    # strict majority -- so the purge is withheld, but acme/a's own retired
    # branch cleanup still applies.
    code = _run_job(engine, _config(repos=["acme/a"]), github)
    assert code == 1
    assert _repo_names(engine) == {"acme/a", "acme/b", "acme/c", "acme/d"}  # purge withheld
    assert _repo_branch_names(engine, "acme/a") == {"main"}  # but 'stale' still retired
