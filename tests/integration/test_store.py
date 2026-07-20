"""Integration tests for indexer.store.index_repo against a real local Postgres.

Reuses the throwaway-schema fixture style from test_db_client.py: a clean schema
per run, the durable-core DDL via ``Base.metadata.create_all``, and a per-connection
``search_path`` that propagates into ``index_repo``'s DML. The fixture connection is
passed directly to ``index_repo`` (the injected-connection seam).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text

from app.db.client import create_db_engine
from app.db.models import INDEX_SEMANTICS_VERSION, Base
from indexer.languages import ExtractedSymbol, IndexCounts, ParsedFile
from indexer.store import StaleIndexError, _stamp_repo, index_repo

SCHEMA = "test_store"


@pytest.fixture
def conn() -> Iterator[Connection]:
    engine = create_db_engine()
    connection = engine.connect()
    try:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        connection.execute(text(f"SET search_path TO {SCHEMA}, public"))
        connection.commit()

        Base.metadata.create_all(bind=connection)
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
) -> list[tuple[ParsedFile, list[ExtractedSymbol]]]:
    return [(_pf(path, content), syms) for path, content, syms in specs]


MAIN = ("main.py", "def f():\n    return 1\n", [ExtractedSymbol("f", "function", 1, 2)])
UTIL = ("util.py", "def g():\n    return 2\n", [ExtractedSymbol("g", "function", 1, 2)])


def _count(conn: Connection, table: str, where: str = "") -> int:
    sql = f"SELECT count(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(text(sql)).scalar_one())


@pytest.mark.integration
def test_first_run_populates_and_stamps_commit(conn: Connection) -> None:
    counts = index_repo(
        conn,
        name="acme/widgets",
        default_branch="main",
        head_sha="sha_first",
        items=_items(MAIN, UTIL),
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0)

    assert _count(conn, "repos") == 1
    assert _count(conn, "files") == 2
    assert _count(conn, "symbols") == 2
    assert _count(conn, "files", "commit <> 'sha_first'") == 0

    repo = conn.execute(
        text("SELECT last_indexed_commit FROM repos WHERE name = 'acme/widgets'")
    ).scalar_one()
    assert repo == "sha_first"


@pytest.mark.integration
def test_rerun_is_idempotent(conn: Connection) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        default_branch="main",
        head_sha="sha_first",
        items=_items(MAIN, UTIL),
    )
    counts = index_repo(
        conn,
        name="acme/widgets",
        default_branch="main",
        head_sha="sha_first",
        items=_items(MAIN, UTIL),
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0)
    assert _count(conn, "repos") == 1
    assert _count(conn, "files") == 2
    assert _count(conn, "symbols") == 2


@pytest.mark.integration
def test_mark_and_sweep_removes_deleted_file(conn: Connection) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        default_branch="main",
        head_sha="sha_first",
        items=_items(MAIN, UTIL),
    )
    removed_file_id = conn.execute(text("SELECT id FROM files WHERE path = 'util.py'")).scalar_one()
    assert _count(conn, "symbols", f"file_id = {removed_file_id}") == 1

    # Reads above autobegan a txn; clear it so index_repo gets a clean connection
    # (production hands a fresh engine.connect() per repo).
    conn.rollback()

    # Re-run without util.py and with a new head SHA -> util.py is swept.
    counts = index_repo(
        conn, name="acme/widgets", default_branch="main", head_sha="sha_second", items=_items(MAIN)
    )
    assert counts == IndexCounts(files=1, symbols=1, swept=1)

    assert _count(conn, "files", "path = 'util.py'") == 0
    assert _count(conn, "symbols", f"file_id = {removed_file_id}") == 0  # cascade
    assert _count(conn, "files") == 1
    assert _count(conn, "files", "commit = 'sha_second'") == 1


@pytest.mark.integration
def test_sweep_is_repo_scoped(conn: Connection) -> None:
    # Two repos indexed at their own SHAs; re-indexing repo A with a new SHA must
    # sweep only A's stale rows and never touch repo B.
    index_repo(
        conn, name="acme/a", default_branch="main", head_sha="a_first", items=_items(MAIN, UTIL)
    )
    conn.rollback()
    index_repo(conn, name="acme/b", default_branch="main", head_sha="b_first", items=_items(UTIL))
    conn.rollback()

    b_files_before = _count(conn, "files", "repo_id = (SELECT id FROM repos WHERE name = 'acme/b')")
    conn.rollback()

    # Re-index A without util.py at a new SHA -> A's util.py swept, B untouched.
    counts = index_repo(
        conn, name="acme/a", default_branch="main", head_sha="a_second", items=_items(MAIN)
    )
    assert counts == IndexCounts(files=1, symbols=1, swept=1)

    assert _count(conn, "files", "repo_id = (SELECT id FROM repos WHERE name = 'acme/b')") == (
        b_files_before
    )
    assert _count(conn, "files", "commit = 'b_first'") == 1  # B's row unchanged


@pytest.mark.integration
def test_stamp_writes_semantics_version(conn: Connection) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        default_branch="main",
        head_sha="sha_first",
        items=_items(MAIN, UTIL),
    )
    conn.rollback()
    # Second run at a new SHA succeeds against its own in-transaction baseline.
    index_repo(
        conn, name="acme/widgets", default_branch="main", head_sha="sha_second", items=_items(MAIN)
    )

    stamp = conn.execute(
        text(
            "SELECT last_indexed_commit, index_semantics_version FROM repos "
            "WHERE name = 'acme/widgets'"
        )
    ).one()
    assert stamp == ("sha_second", INDEX_SEMANTICS_VERSION)


@pytest.mark.integration
def test_legacy_null_semantics_version_is_rewritten(conn: Connection) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        default_branch="main",
        head_sha="sha_first",
        items=_items(MAIN),
    )
    conn.execute(text("UPDATE repos SET index_semantics_version = NULL"))
    conn.commit()

    index_repo(
        conn, name="acme/widgets", default_branch="main", head_sha="sha_second", items=_items(MAIN)
    )
    stamp = conn.execute(
        text(
            "SELECT last_indexed_commit, index_semantics_version FROM repos "
            "WHERE name = 'acme/widgets'"
        )
    ).one()
    assert stamp == ("sha_second", INDEX_SEMANTICS_VERSION)


@pytest.mark.integration
def test_cas_predicate_rejects_wrong_baseline(conn: Connection) -> None:
    # Direct test of the compare-and-set UPDATE predicate: index normally, then
    # run the stamp with a deliberately wrong baseline. Zero rows match -> raise.
    index_repo(
        conn,
        name="acme/widgets",
        default_branch="main",
        head_sha="sha_a",
        items=_items(MAIN, UTIL),
    )
    files_before = _count(conn, "files")
    symbols_before = _count(conn, "symbols")
    repo_id = int(
        conn.execute(text("SELECT id FROM repos WHERE name = 'acme/widgets'")).scalar_one()
    )
    conn.rollback()

    # The raise propagates out of conn.begin(), which rolls the transaction back
    # exactly as it would inside index_repo.
    with pytest.raises(StaleIndexError, match="wrong_sha"), conn.begin():
        conn.execute(text("DELETE FROM files WHERE path = 'util.py'"))
        _stamp_repo(
            conn,
            name="acme/widgets",
            repo_id=repo_id,
            head_sha="sha_b",
            baseline_commit="wrong_sha",
            baseline_version=INDEX_SEMANTICS_VERSION,
        )

    # The aborted transaction left the index exactly as the sha_a run wrote it.
    assert _count(conn, "files") == files_before
    assert _count(conn, "symbols") == symbols_before
    assert _count(conn, "files", "commit <> 'sha_a'") == 0
    assert (
        conn.execute(
            text("SELECT last_indexed_commit FROM repos WHERE name = 'acme/widgets'")
        ).scalar_one()
        == "sha_a"
    )


@pytest.mark.integration
def test_midrun_failure_rolls_back_entirely(conn: Connection) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        default_branch="main",
        head_sha="sha_first",
        items=_items(MAIN, UTIL),
    )
    files_before = _count(conn, "files")
    symbols_before = _count(conn, "symbols")
    conn.rollback()  # clear the read txn before the next index_repo (see note above)

    def poison() -> Iterator[tuple[ParsedFile, list[ExtractedSymbol]]]:
        # First item is a NEW file that would be inserted; then blow up mid-stream
        # so the exception propagates out of index_repo's conn.begin().
        yield _pf("new.py", "def h():\n    return 3\n"), [ExtractedSymbol("h", "function", 1, 2)]
        raise RuntimeError("poison item")

    with pytest.raises(RuntimeError, match="poison item"):
        index_repo(
            conn,
            name="acme/widgets",
            default_branch="main",
            head_sha="sha_third",
            items=poison(),
        )

    # Nothing committed: the new file is absent and prior rows are untouched.
    assert _count(conn, "files", "path = 'new.py'") == 0
    assert _count(conn, "files") == files_before
    assert _count(conn, "symbols") == symbols_before
    assert _count(conn, "files", "commit <> 'sha_first'") == 0
