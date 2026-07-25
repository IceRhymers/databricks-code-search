"""Integration tests for the batched changed/new write path (#105) against real Postgres.

Row-identity and byte-parity proof against real SQL, complementing
``tests/unit/test_store_batching.py``'s statement-level pins. Clones
``tests/integration/test_store_delta.py``'s throwaway-schema idiom (own copy,
per ``tests/integration/AGENTS.md``'s no-conftest convention).

**Deliberately builds no ``chunks`` table and creates no ``lakebase_*``
extension** -- same discipline as ``test_store_delta.py``, for the same reason
(fixtures are module-local by convention, so one failing fixture would
vaporize the whole module). Chunk-touching batched cases live in
``tests/integration/test_chunk_batching.py`` instead.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Connection, event, text

import indexer.store as store_module
from app.db.client import create_db_engine
from app.db.models import INDEX_SEMANTICS_VERSION, Base, File
from indexer.languages import (
    ExtractedEdge,
    ExtractedSymbol,
    FileExtraction,
    IndexCounts,
    ParsedFile,
)
from indexer.store import StaleIndexError, _stamp_repo_branch, index_repo

SCHEMA_PREFIX = "test_store_batching"


@pytest.fixture
def conn() -> Iterator[Connection]:
    with _fresh_schema() as connection:
        yield connection


@contextlib.contextmanager
def _fresh_schema() -> Iterator[Connection]:
    """A uniquely-named throwaway schema + a live connection, torn down after."""
    schema = f"{SCHEMA_PREFIX}_{uuid4().hex[:12]}"
    engine = create_db_engine()
    connection = engine.connect()
    try:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET search_path TO {schema}, public"))
        connection.commit()

        Base.metadata.create_all(bind=connection)
        connection.commit()

        yield connection
    finally:
        connection.rollback()
        connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
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


def _fn(name: str, n: int) -> tuple[str, str, list[ExtractedSymbol]]:
    content = f"def f{n}():\n    return {n}\n"
    return (f"{name}{n}.py", content, [ExtractedSymbol(f"f{n}", "function", 1, 2)])


def _index_default(
    conn: Connection, *, name: str, head_sha: str, items: list[tuple[ParsedFile, FileExtraction]]
) -> IndexCounts:
    return index_repo(
        conn, name=name, branch="main", is_default=True, head_sha=head_sha, items=items
    )


def _count(conn: Connection, table: str, where: str = "") -> int:
    sql = f"SELECT count(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(text(sql)).scalar_one())


def _sym(prefix: str, n: int) -> ExtractedSymbol:
    return ExtractedSymbol(f"{prefix}{n}", "function", 1, 2)


def _fixture_items(branch: str) -> list[tuple[ParsedFile, FileExtraction]]:
    """~15 files: 5 content-deduped across every branch, 2 divergent per branch,
    one zero-symbols file, one file with a real edge, and 6 branch-unique files
    -- enough diversity to exercise every row shape the batched write touches."""
    items: list[tuple[ParsedFile, FileExtraction]] = []

    for i in range(5):
        content = f"def shared{i}():\n    return {i}\n"
        extraction = FileExtraction(symbols=[_sym("shared", i)], edges=[])
        items.append((_pf(f"shared{i}.py", content), extraction))

    for i in range(2):
        # A deterministic per-(branch, i) value -- NOT Python's hash(), which is
        # randomized per-process (PYTHONHASHSEED) and would make this fixture's
        # content non-reproducible across separate runs/processes.
        magic = (sum(ord(c) for c in branch) * 31 + i) % 997
        content = f"def divergent{i}():\n    return {magic}\n"
        extraction = FileExtraction(symbols=[_sym("divergent", i)], edges=[])
        items.append((_pf(f"divergent{i}.py", content), extraction))

    items.append((_pf("nosymbols.py", "# just a comment\n"), FileExtraction(symbols=[], edges=[])))

    caller = _sym("caller", 0)
    items.append(
        (
            _pf(f"{branch}_withedges.py", "def caller0():\n    callee()\n"),
            FileExtraction(
                symbols=[caller],
                edges=[ExtractedEdge(kind="call", target="callee", line=2, enclosing=caller)],
            ),
        )
    )

    for i in range(6):
        content = f"def {branch}_only{i}():\n    return {i}\n"
        extraction = FileExtraction(symbols=[_sym(f"{branch}_only", i)], edges=[])
        items.append((_pf(f"{branch}_only{i}.py", content), extraction))

    return items


def _dump_files(conn: Connection) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        text(
            "SELECT repo_id, path, content_sha, lang, size, content, commit, branches "
            "FROM files ORDER BY path, content_sha"
        )
    ).all()
    return sorted(
        (
            r.repo_id,
            r.path,
            r.content_sha,
            r.lang,
            r.size,
            r.content,
            r.commit,
            tuple(sorted(r.branches)),
        )
        for r in rows
    )


def _dump_symbols(conn: Connection) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        text(
            "SELECT f.path, s.name, s.kind, s.start_line, s.end_line "
            "FROM symbols s JOIN files f ON f.id = s.file_id"
        )
    ).all()
    return sorted(tuple(r) for r in rows)


def _dump_edges(conn: Connection) -> list[tuple[Any, ...]]:
    rows = conn.execute(
        text(
            "SELECT f.path, e.edge_kind, e.target_name, e.line, "
            "e.enclosing_name, e.enclosing_kind, "
            "e.enclosing_start_line, e.enclosing_end_line "
            "FROM reference_edges e JOIN files f ON f.id = e.file_id"
        )
    ).all()
    return sorted(tuple(r) for r in rows)


# --- Test 14: batch-size invariance is the parity harness --------------------


@pytest.mark.integration
def test_batch_size_invariance_produces_a_byte_identical_corpus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index the same 3-branch fixture repo at _BATCH_MAX_FILES in {1, 2, 7, 500}
    into four throwaway schemas; the normalized (files, symbols, edges) dump
    must be identical across all four -- the direct proof that batch size
    changes round trips, never corpus content."""
    dumps = []
    for batch_size in (1, 2, 7, 500):
        monkeypatch.setattr(store_module, "_BATCH_MAX_FILES", batch_size)
        with _fresh_schema() as conn:
            index_repo(
                conn,
                name="acme/widgets",
                branch="a",
                is_default=True,
                head_sha="sha_a",
                items=_fixture_items("a"),
            )
            conn.commit()
            index_repo(
                conn,
                name="acme/widgets",
                branch="b",
                is_default=False,
                head_sha="sha_b",
                items=_fixture_items("b"),
            )
            conn.commit()
            index_repo(
                conn,
                name="acme/widgets",
                branch="c",
                is_default=False,
                head_sha="sha_c",
                items=_fixture_items("c"),
            )
            conn.commit()
            dumps.append((_dump_files(conn), _dump_symbols(conn), _dump_edges(conn)))

    first = dumps[0]
    for batch_size, dump in zip((2, 7, 500), dumps[1:], strict=True):
        assert dump == first, f"corpus at _BATCH_MAX_FILES={batch_size} diverged from size=1"


# --- Test 15: the excluded.* trap, behaviorally -------------------------------


@pytest.mark.integration
def test_excluded_trap_every_conflicting_row_keeps_its_own_values(conn: Connection) -> None:
    """Re-index a batch of 3 files whose contents ALL differ, where every row
    already exists (every row takes DO UPDATE): each row's stored
    content/lang/size/commit must be its OWN, not the last file's. Under the
    literal-binding bug all three collapse to the last file's values."""
    v1 = _items(("a.py", "x = 1\n", []), ("b.py", "y = 2\n", []), ("c.py", "z = 3\n", []))
    _index_default(conn, name="acme/widgets", head_sha="sha1", items=v1)
    conn.commit()

    v2 = _items(("a.py", "x = 100\n", []), ("b.py", "y = 200\n", []), ("c.py", "z = 300\n", []))
    _index_default(conn, name="acme/widgets", head_sha="sha2", items=v2)

    for path, content in [("a.py", "x = 100\n"), ("b.py", "y = 200\n"), ("c.py", "z = 300\n")]:
        row = conn.execute(
            text("SELECT content, commit, size FROM files WHERE path = :p"), {"p": path}
        ).one()
        assert row.content == content
        assert row.commit == "sha2"
        assert row.size == len(content.encode())


# --- Test 15b: column-completeness of the batched upsert's row dict ---------


@pytest.mark.integration
def test_files_upsert_row_dict_covers_every_non_id_column(conn: Connection) -> None:
    """The row-dict key set built by _flush_file_batch must equal every `files`
    column except `id` -- catches a column silently dropped uniformly at every
    batch size, which test 14's batched-against-batched comparison cannot."""
    captured_sql: list[str] = []

    def _listener(
        conn_: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if statement.startswith("INSERT INTO files"):
            captured_sql.append(statement)

    event.listen(conn.engine, "before_cursor_execute", _listener)
    try:
        _index_default(
            conn,
            name="acme/widgets",
            head_sha="sha1",
            items=_items(("a.py", "x = 1\n", []), ("b.py", "y = 2\n", [])),
        )
    finally:
        event.remove(conn.engine, "before_cursor_execute", _listener)

    assert captured_sql
    match = re.search(r"INSERT INTO files \(([^)]+)\)", captured_sql[0])
    assert match is not None
    columns = {c.strip() for c in match.group(1).split(",")}
    assert columns == set(File.__table__.columns.keys()) - {"id"}


# --- Test 16: mid-batch rollback ----------------------------------------------


@pytest.mark.integration
def test_mid_batch_generator_failure_rolls_back_the_whole_transaction(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `items` generator that raises AFTER at least one flush already ran
    (_BATCH_MAX_FILES=5, 6 files yielded before the raise) leaves ZERO
    files/symbols/reference_edges rows committed and no repo_branches row --
    the whole (repo, branch) transaction rolls back, not just the failing batch."""
    monkeypatch.setattr(store_module, "_BATCH_MAX_FILES", 5)

    class _Boom(Exception):
        pass

    def _raising_items() -> Iterator[tuple[ParsedFile, FileExtraction]]:
        for i in range(6):
            yield _pf(f"f{i}.py", f"x = {i}\n"), FileExtraction(symbols=[], edges=[])
        raise _Boom("simulated failure mid-generator, after the first flush")

    with pytest.raises(_Boom):
        index_repo(
            conn,
            name="acme/widgets",
            branch="main",
            is_default=True,
            head_sha="sha1",
            items=_raising_items(),
        )

    assert _count(conn, "files") == 0
    assert _count(conn, "symbols") == 0
    assert _count(conn, "reference_edges") == 0
    assert _count(conn, "repo_branches") == 0


# --- Test 17: statement count, measured ---------------------------------------


@pytest.mark.integration
def test_statement_count_per_file_meets_the_acceptance_criterion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1: round trips per file drop from 3-7 to <= 0.05. Measures real cursor
    executions over a 600-file first-time index, both batched and at
    _BATCH_MAX_FILES=1, and prints the measured value for the PR body."""
    n = 600
    fixture_items = _items(*[_fn("f", i) for i in range(n)])

    def _count_statements(batch_size: int) -> int:
        monkeypatch.setattr(store_module, "_BATCH_MAX_FILES", batch_size)
        with _fresh_schema() as conn:
            count = 0

            def _listener(
                conn_: Any,
                cursor: Any,
                statement: str,
                parameters: Any,
                context: Any,
                executemany: bool,
            ) -> None:
                nonlocal count
                count += 1

            event.listen(conn.engine, "before_cursor_execute", _listener)
            try:
                index_repo(
                    conn,
                    name="acme/widgets",
                    branch="main",
                    is_default=True,
                    head_sha="sha1",
                    items=fixture_items,
                )
            finally:
                event.remove(conn.engine, "before_cursor_execute", _listener)
            return count

    batched = _count_statements(500)
    unbatched = _count_statements(1)

    # Fixed per-transaction overhead independent of N and of batch size:
    # repos-insert, repo_branches-insert, sweep-update, sweep-delete, stamp.
    fixed_overhead = 5
    per_file = (batched - fixed_overhead) / n
    print(f"\nstatements/file at _BATCH_MAX_FILES=500, N={n}: {per_file:.4f} (issue AC: <= 0.05)")
    assert per_file <= 0.05
    assert batched < unbatched


# --- Test 18: branch union survives the multi-row form, gate closed ---------


@pytest.mark.integration
def test_branch_union_survives_the_multi_row_form_with_the_gate_closed(conn: Connection) -> None:
    """Branch 'b' indexes a batch of files whose exact (path, content) already
    exist under branch 'a'. 'b' has never indexed before (baseline_version is
    NULL), so the delta gate is CLOSED and every file takes the changed/new
    batched path -- not membership-only. Every such row's `branches` must end
    ['a', 'b'], sorted-distinct: the multi-row array-union SET, not a
    per-row overwrite."""
    shared = _items(*[_fn("shared", i) for i in range(5)])
    index_repo(
        conn, name="acme/widgets", branch="a", is_default=True, head_sha="sha_a", items=shared
    )
    conn.commit()

    index_repo(
        conn, name="acme/widgets", branch="b", is_default=False, head_sha="sha_b", items=shared
    )

    rows = conn.execute(text("SELECT path, branches FROM files")).all()
    assert len(rows) == 5
    for row in rows:
        assert sorted(row.branches) == ["a", "b"]


# --- Test 19: sweep and CAS still hold under batching ------------------------


@pytest.mark.integration
def test_sweep_still_removes_a_deleted_file_under_batching(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "_BATCH_MAX_FILES", 2)
    items_v1 = _items(*[_fn("f", i) for i in range(5)])
    index_repo(
        conn, name="acme/widgets", branch="main", is_default=True, head_sha="sha1", items=items_v1
    )
    conn.commit()

    items_v2 = _items(*[_fn("f", i) for i in range(4)])  # f4.py dropped
    counts = index_repo(
        conn, name="acme/widgets", branch="main", is_default=True, head_sha="sha2", items=items_v2
    )
    assert counts.swept == 1
    assert _count(conn, "files", "path = 'f4.py'") == 0


@pytest.mark.integration
def test_cas_conflict_rolls_back_the_whole_batched_transaction(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "_BATCH_MAX_FILES", 2)
    items = _items(*[_fn("f", i) for i in range(5)])
    index_repo(
        conn, name="acme/widgets", branch="main", is_default=True, head_sha="sha1", items=items
    )
    files_before = _count(conn, "files")
    repo_id = int(
        conn.execute(text("SELECT id FROM repos WHERE name = 'acme/widgets'")).scalar_one()
    )
    conn.rollback()

    with pytest.raises(StaleIndexError, match="wrong_sha"), conn.begin():
        conn.execute(text("DELETE FROM files WHERE path = 'f0.py'"))
        _stamp_repo_branch(
            conn,
            name="acme/widgets",
            branch="main",
            repo_id=repo_id,
            head_sha="sha2",
            baseline_commit="wrong_sha",
            baseline_version=INDEX_SEMANTICS_VERSION,
        )
    assert _count(conn, "files") == files_before


@pytest.mark.integration
def test_empty_items_skips_sweep_and_holds_the_semantics_version_under_batching(
    conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "_BATCH_MAX_FILES", 2)
    items = _items(*[_fn("f", i) for i in range(5)])
    index_repo(
        conn, name="acme/widgets", branch="main", is_default=True, head_sha="sha1", items=items
    )
    conn.execute(text("UPDATE repo_branches SET index_semantics_version = 3"))
    conn.commit()

    counts = index_repo(
        conn, name="acme/widgets", branch="main", is_default=True, head_sha="sha2", items=[]
    )
    assert counts == IndexCounts(files=0, symbols=0, swept=0, edges=0)
    stamp = conn.execute(
        text("SELECT last_indexed_commit, index_semantics_version FROM repo_branches")
    ).one()
    assert stamp == ("sha2", 3)
