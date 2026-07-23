"""Integration tests for indexer.store.reconcile_retired_branches against a real local Postgres.

Reuses test_store.py's throwaway-schema fixture style, extended with the same
raw ``chunks`` DDL as test_store_chunk_writer.py (``chunks`` is deliberately
outside ``Base.metadata`` -- see app/db/semantic.py) so cascade deletes can be
proven for both ``symbols`` and ``chunks`` in one place.

This is a pure storage primitive for corpus reconciliation, exercised
directly here rather than through the job wiring that consumes it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app import service
from app.config import SEMANTIC_EMBEDDING_DIM, Settings
from app.db.client import create_db_engine
from app.db.grants import build_job_grants
from app.db.models import Base
from indexer.chunk_store import write_chunks
from indexer.languages import ExtractedSymbol, FileExtraction, ParsedFile
from indexer.store import (
    ReconcileCounts,
    index_repo,
    reconcile_removed_repos,
    reconcile_retired_branches,
)

SCHEMA = "test_reconcile"


@pytest.fixture
def conn() -> Iterator[Connection]:
    engine = create_db_engine()
    connection = engine.connect()
    try:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        connection.execute(text(f"SET search_path TO {SCHEMA}, public"))
        connection.commit()

        Base.metadata.create_all(bind=connection)
        connection.execute(
            text(
                "CREATE TABLE chunks ("
                "id bigserial PRIMARY KEY, "
                "file_id integer NOT NULL REFERENCES files(id) ON DELETE CASCADE, "
                "chunk_index integer NOT NULL, "
                "content text NOT NULL, "
                "start_line integer, "
                "end_line integer, "
                f"embedding vector({SEMANTIC_EMBEDDING_DIM}), "
                "ts tsvector, "
                "CONSTRAINT uq_chunks_file_id_chunk_index UNIQUE (file_id, chunk_index))"
            )
        )
        connection.commit()

        yield connection
    finally:
        connection.rollback()
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.commit()
        connection.close()
        engine.dispose()


def _pf(path: str, content: str) -> ParsedFile:
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


def _items(
    *specs: tuple[str, str, list[ExtractedSymbol]],
) -> list[tuple[ParsedFile, FileExtraction]]:
    return [
        (_pf(path, content), FileExtraction(symbols=syms, edges=[]))
        for path, content, syms in specs
    ]


MAIN = ("main.py", "def f():\n    return 1\n", [ExtractedSymbol("f", "function", 1, 2)])
UTIL = ("util.py", "def g():\n    return 2\n", [ExtractedSymbol("g", "function", 1, 2)])
FEATURE_ONLY = (
    "only_feature.py",
    "def h():\n    return 3\n",
    [ExtractedSymbol("h", "function", 1, 2)],
)

_STUB_VECTOR = [0.1] * SEMANTIC_EMBEDDING_DIM


def _stub_chunk_writer(conn: Connection, repo_id: int, file_id: int, pf: ParsedFile) -> None:
    write_chunks(conn, file_id=file_id, chunks=[(0, pf.content, 1, 2, _STUB_VECTOR)])


def _count(conn: Connection, table: str, where: str = "") -> int:
    sql = f"SELECT count(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(text(sql)).scalar_one())


def _file_id(conn: Connection, path: str, repo: str = "acme/widgets") -> int:
    return int(
        conn.execute(
            text(
                "SELECT f.id FROM files f JOIN repos r ON r.id = f.repo_id "
                "WHERE f.path = :p AND r.name = :repo"
            ),
            {"p": path, "repo": repo},
        ).scalar_one()
    )


def _branches_of(conn: Connection, path: str, repo: str = "acme/widgets") -> list[str]:
    return list(
        conn.execute(
            text(
                "SELECT f.branches FROM files f JOIN repos r ON r.id = f.repo_id "
                "WHERE f.path = :p AND r.name = :repo"
            ),
            {"p": path, "repo": repo},
        ).scalar_one()
    )


def _repo_names(conn: Connection) -> set[str]:
    return set(conn.execute(text("SELECT name FROM repos")).scalars().all())


def _cfg() -> Settings:
    return Settings(
        lakebase_endpoint=None,
        statement_timeout_ms=5000,
        max_content_bytes=8 * 1024 * 1024,
        row_limit=200,
        max_row_limit=1000,
        semantic_enabled=False,
    )


def _seed_reference_edge(
    conn: Connection, *, repo_id: int, file_id: int, target_name: str = "target_fn"
) -> None:
    """Seed one raw reference edge row directly.

    These reconcile tests exercise the storage primitives (retirement/purge
    cascades) in isolation from the real extractor, which landed in #84
    (``indexer.symbols.extract_file`` / ``indexer.store.index_repo``'s edge
    writer) -- seeding a row by hand keeps this module focused on
    ``reconcile_retired_branches``/``reconcile_removed_repos`` alone.
    """
    conn.execute(
        text(
            "INSERT INTO reference_edges (repo_id, file_id, edge_kind, target_name, line) "
            "VALUES (:r, :f, 'call', :name, 1)"
        ),
        {"r": repo_id, "f": file_id, "name": target_name},
    )


def _repo_id(conn: Connection, name: str) -> int:
    return int(conn.execute(text("SELECT id FROM repos WHERE name = :n"), {"n": name}).scalar_one())


def _repo_branch_names(conn: Connection, name: str) -> set[str]:
    rows = (
        conn.execute(
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


class _FailOnStatementConnection:
    """Wraps a real Connection and raises when a statement's text contains ``poison``.

    Used to prove atomicity: the last of reconcile's four statements is the
    ``repo_branches`` DELETE, so poisoning it proves every prior mutation in
    the same transaction (the files UPDATE and DELETE) rolls back too.
    """

    def __init__(self, conn: Connection, poison: str) -> None:
        self._conn = conn
        self._poison = poison

    def execute(self, clause: Any, *args: Any, **kwargs: Any) -> Any:
        if self._poison in str(clause):
            raise RuntimeError("injected failure")
        return self._conn.execute(clause, *args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._conn, item)


class _ExecuteThenFailConnection:
    """Wraps a real Connection: runs the statement for real, THEN raises if it matches ``poison``.

    Stronger than ``_FailOnStatementConnection`` for a single-statement helper like
    ``reconcile_removed_repos``: poisoning before execution would prove nothing (the
    DELETE never ran), so this lets the DELETE...RETURNING and its cascades genuinely
    fire in-transaction, THEN fails -- proving ``conn.begin()``'s rollback restores
    everything the statement (and its FK cascades) just did.
    """

    def __init__(self, conn: Connection, poison: str) -> None:
        self._conn = conn
        self._poison = poison

    def execute(self, clause: Any, *args: Any, **kwargs: Any) -> Any:
        result = self._conn.execute(clause, *args, **kwargs)
        if self._poison in str(clause):
            raise RuntimeError("injected failure after execute")
        return result

    def __getattr__(self, item: str) -> Any:
        return getattr(self._conn, item)


@pytest.mark.integration
def test_shared_content_row_survives_and_remains_searchable(conn: Connection) -> None:
    """A retired branch's array-membership is stripped; a still-live branch keeps searching it."""
    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_main",
        items=_items(MAIN, UTIL),
        chunk_writer=_stub_chunk_writer,
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="feature",
        is_default=False,
        head_sha="sha_feature",
        items=_items(MAIN, FEATURE_ONLY),
        chunk_writer=_stub_chunk_writer,
    )
    conn.rollback()

    assert sorted(_branches_of(conn, "main.py")) == ["feature", "main"]
    conn.rollback()  # clear the read's autobegun txn before reconcile's own conn.begin()

    counts = reconcile_retired_branches(conn, name="acme/widgets", retired_branches=["feature"])

    assert counts == ReconcileCounts(branches_removed=1, files_stripped=2, files_deleted=1)

    # main.py was shared -> row survives, membership narrowed to just 'main', and
    # it remains genuinely searchable by a branch-filtered = ANY(branches) read.
    assert _count(conn, "files", "path = 'main.py'") == 1
    assert _branches_of(conn, "main.py") == ["main"]
    assert (
        _count(
            conn,
            "files",
            "path = 'main.py' AND 'main' = ANY(branches)",
        )
        == 1
    )

    # util.py never had 'feature' membership -> completely untouched.
    assert _branches_of(conn, "util.py") == ["main"]


@pytest.mark.integration
def test_divergent_branch_only_file_is_deleted_with_symbols_and_chunks_cascade(
    conn: Connection,
) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_main",
        items=_items(MAIN),
        chunk_writer=_stub_chunk_writer,
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="feature",
        is_default=False,
        head_sha="sha_feature",
        items=_items(FEATURE_ONLY),
        chunk_writer=_stub_chunk_writer,
    )
    conn.rollback()

    repo_id = _repo_id(conn, "acme/widgets")
    feature_file_id = _file_id(conn, "only_feature.py")
    main_file_id = _file_id(conn, "main.py")
    _seed_reference_edge(conn, repo_id=repo_id, file_id=feature_file_id, target_name="retired_fn")
    _seed_reference_edge(conn, repo_id=repo_id, file_id=main_file_id, target_name="surviving_fn")
    conn.commit()
    assert _count(conn, "symbols", f"file_id = {feature_file_id}") == 1
    assert _count(conn, "chunks", f"file_id = {feature_file_id}") == 1
    assert _count(conn, "reference_edges", f"file_id = {feature_file_id}") == 1
    conn.rollback()  # clear the reads' autobegun txn before reconcile's own conn.begin()

    counts = reconcile_retired_branches(conn, name="acme/widgets", retired_branches=["feature"])

    assert counts == ReconcileCounts(branches_removed=1, files_stripped=1, files_deleted=1)
    assert _count(conn, "files", "path = 'only_feature.py'") == 0
    assert _count(conn, "symbols", f"file_id = {feature_file_id}") == 0  # cascade
    assert _count(conn, "chunks", f"file_id = {feature_file_id}") == 0  # cascade
    assert _count(conn, "reference_edges", f"file_id = {feature_file_id}") == 0  # cascade

    # main.py's branch ('main') was never retired -> untouched, including its edge row.
    assert _count(conn, "files", "path = 'main.py'") == 1
    assert _branches_of(conn, "main.py") == ["main"]
    assert _count(conn, "reference_edges", f"file_id = {main_file_id}") == 1


@pytest.mark.integration
def test_multiple_retired_branches_removed_in_one_atomic_call(conn: Connection) -> None:
    index_repo(
        conn, name="acme/widgets", branch="a", is_default=True, head_sha="sha_a", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b",
        items=_items(MAIN),
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="c",
        is_default=False,
        head_sha="sha_c",
        items=_items(MAIN),
    )
    conn.rollback()

    assert sorted(_branches_of(conn, "main.py")) == ["a", "b", "c"]
    conn.rollback()  # clear the read's autobegun txn before reconcile's own conn.begin()

    counts = reconcile_retired_branches(conn, name="acme/widgets", retired_branches=["b", "c"])

    assert counts == ReconcileCounts(branches_removed=2, files_stripped=1, files_deleted=0)
    assert _branches_of(conn, "main.py") == ["a"]
    assert _repo_branch_names(conn, "acme/widgets") == {"a"}


@pytest.mark.integration
def test_reconcile_is_repo_scoped(conn: Connection) -> None:
    index_repo(
        conn,
        name="acme/a",
        branch="feature",
        is_default=False,
        head_sha="sha_a",
        items=_items(MAIN),
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/a",
        branch="main",
        is_default=True,
        head_sha="sha_a_main",
        items=_items(MAIN),
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/b",
        branch="feature",
        is_default=False,
        head_sha="sha_b",
        items=_items(MAIN),
    )
    conn.rollback()

    counts = reconcile_retired_branches(conn, name="acme/a", retired_branches=["feature"])
    assert counts == ReconcileCounts(branches_removed=1, files_stripped=1, files_deleted=0)

    # acme/a's main.py loses 'feature' membership...
    assert _branches_of(conn, "main.py", repo="acme/a") == ["main"]
    # ...while acme/b's same-path, same-named branch is completely untouched.
    assert _branches_of(conn, "main.py", repo="acme/b") == ["feature"]
    assert _repo_branch_names(conn, "acme/b") == {"feature"}


@pytest.mark.integration
def test_missing_repo_and_empty_retired_set_are_safe_noops(conn: Connection) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_a",
        items=_items(MAIN),
    )
    conn.rollback()

    files_before = _count(conn, "files")
    branches_before = _repo_branch_names(conn, "acme/widgets")
    conn.rollback()  # clear the reads' autobegun txn before either reconcile call below

    # Empty retired set against a real, existing repo.
    counts = reconcile_retired_branches(conn, name="acme/widgets", retired_branches=[])
    assert counts == ReconcileCounts(branches_removed=0, files_stripped=0, files_deleted=0)

    # A nonexistent repo name, with a genuinely retired-looking branch name.
    counts = reconcile_retired_branches(conn, name="does/not-exist", retired_branches=["main"])
    assert counts == ReconcileCounts(branches_removed=0, files_stripped=0, files_deleted=0)

    assert _count(conn, "files") == files_before
    assert _repo_branch_names(conn, "acme/widgets") == branches_before


@pytest.mark.integration
def test_injected_failure_rolls_back_every_prior_mutation(conn: Connection) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_main",
        items=_items(MAIN),
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="feature",
        is_default=False,
        head_sha="sha_feature",
        items=_items(MAIN, FEATURE_ONLY),
    )
    conn.rollback()

    branches_before = _branches_of(conn, "main.py")
    repo_branches_before = _repo_branch_names(conn, "acme/widgets")
    files_before = _count(conn, "files")
    conn.rollback()  # clear the reads' autobegun txn before reconcile's own conn.begin()

    poisoned = _FailOnStatementConnection(conn, poison="DELETE FROM repo_branches")
    with pytest.raises(RuntimeError, match="injected failure"):
        reconcile_retired_branches(poisoned, name="acme/widgets", retired_branches=["feature"])  # type: ignore[arg-type]

    # Nothing committed: the files UPDATE and DELETE that ran before the
    # poisoned repo_branches DELETE are rolled back along with it.
    assert _branches_of(conn, "main.py") == branches_before
    assert _repo_branch_names(conn, "acme/widgets") == repo_branches_before
    assert _count(conn, "files") == files_before
    assert _count(conn, "files", "path = 'only_feature.py'") == 1


@pytest.mark.integration
def test_reconcile_runs_under_the_actual_job_role(conn: Connection) -> None:
    job_role = f"test_job_wr_{uuid.uuid4().hex[:12]}"
    conn.execute(text(f"CREATE ROLE {job_role} NOLOGIN"))
    for stmt in build_job_grants(SCHEMA, job_role):
        conn.execute(text(stmt))
    conn.commit()

    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_main",
        items=_items(MAIN),
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="feature",
        is_default=False,
        head_sha="sha_feature",
        items=_items(MAIN),
    )
    conn.commit()

    try:
        conn.execute(text(f"SET ROLE {job_role}"))
        conn.execute(text(f"SET search_path TO {SCHEMA}, public"))
        conn.commit()

        counts = reconcile_retired_branches(conn, name="acme/widgets", retired_branches=["feature"])
        conn.commit()

        assert counts == ReconcileCounts(branches_removed=1, files_stripped=1, files_deleted=0)
        assert _branches_of(conn, "main.py") == ["main"]
    finally:
        conn.rollback()
        conn.execute(text("RESET ROLE"))
        conn.execute(text(f"DROP OWNED BY {job_role} CASCADE"))
        conn.execute(text(f"DROP ROLE IF EXISTS {job_role}"))
        conn.commit()


@pytest.mark.integration
def test_reconcile_removed_repos_purges_repo_and_cascades_all_six_tables(
    conn: Connection,
) -> None:
    index_repo(
        conn,
        name="acme/kept",
        branch="main",
        is_default=True,
        head_sha="sha_kept",
        items=_items(MAIN),
        chunk_writer=_stub_chunk_writer,
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/removed",
        branch="main",
        is_default=True,
        head_sha="sha_removed",
        items=_items(FEATURE_ONLY),
        chunk_writer=_stub_chunk_writer,
    )
    conn.rollback()

    # Capture the victim's file_id(s) BEFORE the purge -- once the repo row is gone,
    # post-hoc reconstruction via a repo-name JOIN is impossible.
    removed_repo_id = _repo_id(conn, "acme/removed")
    removed_file_id = _file_id(conn, "only_feature.py", repo="acme/removed")
    _seed_reference_edge(conn, repo_id=removed_repo_id, file_id=removed_file_id)
    conn.commit()
    assert _count(conn, "symbols", f"file_id = {removed_file_id}") == 1
    assert _count(conn, "chunks", f"file_id = {removed_file_id}") == 1
    assert _count(conn, "reference_edges", f"file_id = {removed_file_id}") == 1
    conn.rollback()  # clear the reads' autobegun txn before reconcile's own conn.begin()

    deleted = reconcile_removed_repos(conn, desired_repos=["acme/kept"])

    assert deleted == ["acme/removed"]
    assert _count(conn, "repos", "name = 'acme/removed'") == 0
    assert _repo_branch_names(conn, "acme/removed") == set()
    assert _count(conn, "files", "path = 'only_feature.py'") == 0
    assert _count(conn, "symbols", f"file_id = {removed_file_id}") == 0  # cascade
    assert _count(conn, "chunks", f"file_id = {removed_file_id}") == 0  # cascade
    assert _count(conn, "reference_edges", f"file_id = {removed_file_id}") == 0  # cascade

    # acme/kept is completely untouched.
    assert _count(conn, "repos", "name = 'acme/kept'") == 1
    assert _count(conn, "files", "path = 'main.py'") == 1
    assert _branches_of(conn, "main.py", repo="acme/kept") == ["main"]
    assert _repo_branch_names(conn, "acme/kept") == {"main"}


@pytest.mark.integration
def test_reconcile_removed_repos_cross_repo_boundary_same_path_and_content(
    conn: Connection,
) -> None:
    """Two repos share an identical (path, content) -- distinct rows via repo_id, not shared."""
    index_repo(
        conn, name="acme/kept", branch="main", is_default=True, head_sha="sha_a", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/removed",
        branch="main",
        is_default=True,
        head_sha="sha_b",
        items=_items(MAIN),
    )
    conn.rollback()

    kept_file_id = _file_id(conn, "main.py", repo="acme/kept")
    removed_file_id = _file_id(conn, "main.py", repo="acme/removed")
    assert kept_file_id != removed_file_id
    conn.rollback()

    deleted = reconcile_removed_repos(conn, desired_repos=["acme/kept"])

    assert deleted == ["acme/removed"]
    assert _count(conn, "files", f"id = {kept_file_id}") == 1
    assert _count(conn, "files", f"id = {removed_file_id}") == 0
    assert _branches_of(conn, "main.py", repo="acme/kept") == ["main"]


@pytest.mark.integration
def test_reconcile_removed_repos_multi_branch_victim_loses_every_repo_branch_row(
    conn: Connection,
) -> None:
    index_repo(
        conn, name="acme/kept", branch="main", is_default=True, head_sha="sha_k", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/removed",
        branch="main",
        is_default=True,
        head_sha="sha_r1",
        items=_items(MAIN),
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/removed",
        branch="feature",
        is_default=False,
        head_sha="sha_r2",
        items=_items(MAIN, FEATURE_ONLY),
    )
    conn.rollback()

    assert _repo_branch_names(conn, "acme/removed") == {"main", "feature"}
    conn.rollback()

    deleted = reconcile_removed_repos(conn, desired_repos=["acme/kept"])

    assert deleted == ["acme/removed"]
    assert _repo_branch_names(conn, "acme/removed") == set()
    assert _repo_branch_names(conn, "acme/kept") == {"main"}


@pytest.mark.integration
def test_reconcile_removed_repos_is_idempotent(conn: Connection) -> None:
    index_repo(
        conn, name="acme/kept", branch="main", is_default=True, head_sha="sha_k", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/removed",
        branch="main",
        is_default=True,
        head_sha="sha_r",
        items=_items(FEATURE_ONLY),
    )
    conn.rollback()

    first = reconcile_removed_repos(conn, desired_repos=["acme/kept"])
    assert first == ["acme/removed"]

    second = reconcile_removed_repos(conn, desired_repos=["acme/kept"])
    assert second == []
    assert _repo_names(conn) == {"acme/kept"}


@pytest.mark.integration
def test_reconcile_removed_repos_all_desired_present_is_a_noop(conn: Connection) -> None:
    index_repo(
        conn, name="acme/a", branch="main", is_default=True, head_sha="sha_a", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn, name="acme/b", branch="main", is_default=True, head_sha="sha_b", items=_items(UTIL)
    )
    conn.rollback()

    repos_before = _repo_names(conn)
    conn.rollback()

    deleted = reconcile_removed_repos(conn, desired_repos=["acme/a", "acme/b"])

    assert deleted == []
    assert _repo_names(conn) == repos_before


@pytest.mark.integration
def test_reconcile_removed_repos_two_victims_returns_sorted_names(conn: Connection) -> None:
    index_repo(
        conn, name="acme/kept", branch="main", is_default=True, head_sha="sha_k", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn, name="acme/y", branch="main", is_default=True, head_sha="sha_y", items=_items(UTIL)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/x",
        branch="main",
        is_default=True,
        head_sha="sha_x",
        items=_items(FEATURE_ONLY),
    )
    conn.rollback()

    deleted = reconcile_removed_repos(conn, desired_repos=["acme/kept"])

    assert deleted == ["acme/x", "acme/y"]


@pytest.mark.integration
def test_reconcile_removed_repos_empty_desired_set_raises_and_corpus_is_intact(
    conn: Connection,
) -> None:
    index_repo(
        conn, name="acme/kept", branch="main", is_default=True, head_sha="sha_k", items=_items(MAIN)
    )
    conn.rollback()

    repos_before = _repo_names(conn)
    conn.rollback()

    with pytest.raises(ValueError, match="must not be empty"):
        reconcile_removed_repos(conn, desired_repos=[])

    assert _repo_names(conn) == repos_before


@pytest.mark.integration
def test_reconcile_removed_repos_injected_failure_after_delete_rolls_back(
    conn: Connection,
) -> None:
    index_repo(
        conn, name="acme/kept", branch="main", is_default=True, head_sha="sha_k", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/removed",
        branch="main",
        is_default=True,
        head_sha="sha_r",
        items=_items(FEATURE_ONLY),
    )
    conn.rollback()

    repos_before = _repo_names(conn)
    files_before = _count(conn, "files")
    conn.rollback()

    poisoned = _ExecuteThenFailConnection(conn, poison="DELETE FROM repos")
    with pytest.raises(RuntimeError, match="injected failure"):
        reconcile_removed_repos(poisoned, desired_repos=["acme/kept"])  # type: ignore[arg-type]

    # The DELETE...RETURNING and its FK cascades genuinely fired in-transaction; the
    # subsequent raise inside `with conn.begin():` rolls all of it back.
    assert _repo_names(conn) == repos_before
    assert _count(conn, "files") == files_before
    assert _count(conn, "files", "path = 'only_feature.py'") == 1


@pytest.mark.integration
def test_reconcile_removed_repos_runs_under_the_actual_job_role(conn: Connection) -> None:
    job_role = f"test_job_rr_{uuid.uuid4().hex[:12]}"
    conn.execute(text(f"CREATE ROLE {job_role} NOLOGIN"))
    for stmt in build_job_grants(SCHEMA, job_role):
        conn.execute(text(stmt))
    conn.commit()

    index_repo(
        conn, name="acme/kept", branch="main", is_default=True, head_sha="sha_k", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/removed",
        branch="main",
        is_default=True,
        head_sha="sha_r",
        items=_items(FEATURE_ONLY),
    )
    conn.commit()

    try:
        conn.execute(text(f"SET ROLE {job_role}"))
        conn.execute(text(f"SET search_path TO {SCHEMA}, public"))
        conn.commit()

        deleted = reconcile_removed_repos(conn, desired_repos=["acme/kept"])
        conn.commit()

        assert deleted == ["acme/removed"]
        assert _repo_names(conn) == {"acme/kept"}
    finally:
        conn.rollback()
        conn.execute(text("RESET ROLE"))
        conn.execute(text(f"DROP OWNED BY {job_role} CASCADE"))
        conn.execute(text(f"DROP ROLE IF EXISTS {job_role}"))
        conn.commit()


@pytest.mark.integration
def test_reconcile_removed_repos_purge_is_visible_to_the_serving_surface(
    conn: Connection,
) -> None:
    """The purge commits via conn.begin(), so a second connection sees it (list_repos_payload)."""
    index_repo(
        conn, name="acme/kept", branch="main", is_default=True, head_sha="sha_k", items=_items(MAIN)
    )
    conn.commit()
    index_repo(
        conn,
        name="acme/removed",
        branch="main",
        is_default=True,
        head_sha="sha_r",
        items=_items(FEATURE_ONLY),
    )
    conn.commit()

    deleted = reconcile_removed_repos(conn, desired_repos=["acme/kept"])
    conn.commit()
    assert deleted == ["acme/removed"]

    prev_pgoptions = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = f"-c search_path={SCHEMA},public"
    engine = create_db_engine()
    try:
        payload = service.list_repos_payload(engine, _cfg())
    finally:
        engine.dispose()
        if prev_pgoptions is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = prev_pgoptions

    names = {repo["name"] for repo in payload["repos"]}
    assert names == {"acme/kept"}
