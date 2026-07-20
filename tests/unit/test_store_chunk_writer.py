"""Unit tests for indexer.store's optional chunk_writer param (issue #14 Phase 2).

A hand-rolled fake Connection stands in for Postgres so this stays a true unit
test -- index_repo's core upsert/sweep/rollback behavior already has DB-backed
coverage in tests/integration/test_store.py. This only proves the NEW surface:
(a) chunk_writer defaults to None, which is byte-identical to the pre-Phase-2
core path (AC-1), and (b) when given, it is called once per file, inside the
same conn.begin(), with (repo_id, file_id, pf).
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from sqlalchemy import Delete, Insert, Update

from indexer.languages import ExtractedSymbol, IndexCounts, ParsedFile
from indexer.store import StaleIndexError, index_repo


class _FakeResult:
    def __init__(self, *, scalar: Any = None, rowcount: int = 0, row: Any = None) -> None:
        self._scalar = scalar
        self._row = row
        self.rowcount = rowcount

    def scalar_one(self) -> Any:
        return self._scalar

    def one(self) -> Any:
        return self._row


class _FakeConn:
    """Just enough of sqlalchemy.Connection for index_repo's fixed statement shape."""

    def __init__(self, *, stamp_rowcount: int = 1) -> None:
        self._next_file_id = 1
        self._stamp_rowcount = stamp_rowcount

    def begin(self) -> Any:
        return contextlib.nullcontext()

    def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        table = stmt.table.name
        if isinstance(stmt, Insert) and table == "repos":
            # (id, last_indexed_commit, index_semantics_version) -- new-repo shape.
            return _FakeResult(row=(1, None, None))
        if isinstance(stmt, Insert) and table == "files":
            file_id = self._next_file_id
            self._next_file_id += 1
            return _FakeResult(scalar=file_id)
        if isinstance(stmt, Insert) and table == "symbols":
            return _FakeResult()
        if isinstance(stmt, Delete) and table == "symbols":
            return _FakeResult()
        if isinstance(stmt, Delete) and table == "files":
            return _FakeResult(rowcount=0)
        if isinstance(stmt, Update) and table == "repos":
            return _FakeResult(rowcount=self._stamp_rowcount)
        raise AssertionError(f"unexpected statement against {table!r}: {stmt}")


def _pf(path: str, content: str) -> ParsedFile:
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


@pytest.mark.unit
def test_chunk_writer_defaults_to_none_and_behavior_is_unchanged() -> None:
    items = [(_pf("a.py", "x = 1\n"), [ExtractedSymbol("x", "variable", 1, 1)])]
    counts = index_repo(
        _FakeConn(), name="acme/widgets", default_branch="main", head_sha="sha1", items=items
    )
    assert counts == IndexCounts(files=1, symbols=1, swept=0)


@pytest.mark.unit
def test_chunk_writer_is_called_once_per_file_with_repo_id_and_file_id() -> None:
    calls: list[tuple[int, int, str]] = []

    def chunk_writer(conn: Any, repo_id: int, file_id: int, pf: ParsedFile) -> None:
        calls.append((repo_id, file_id, pf.path))

    items = [
        (_pf("a.py", "x = 1\n"), []),
        (_pf("b.py", "y = 2\n"), []),
    ]
    index_repo(
        _FakeConn(),
        name="acme/widgets",
        default_branch="main",
        head_sha="sha1",
        items=items,
        chunk_writer=chunk_writer,
    )
    assert calls == [(1, 1, "a.py"), (1, 2, "b.py")]


@pytest.mark.unit
def test_no_chunk_writer_means_no_extra_calls() -> None:
    # A None chunk_writer must never itself be invoked (it isn't callable).
    items = [(_pf("a.py", "x = 1\n"), [])]
    # No AttributeError/TypeError from trying to call None -> proves the `if
    # chunk_writer is not None` guard is doing its job.
    index_repo(
        _FakeConn(),
        name="acme/widgets",
        default_branch="main",
        head_sha="sha1",
        items=items,
        chunk_writer=None,
    )


@pytest.mark.unit
def test_stamp_matching_no_row_raises_stale_index_error() -> None:
    # The CAS UPDATE matching zero rows means the repos row moved out from under
    # the statement-1 baseline; index_repo must abort rather than stamp.
    items = [(_pf("a.py", "x = 1\n"), [])]
    with pytest.raises(StaleIndexError, match="acme/widgets"):
        index_repo(
            _FakeConn(stamp_rowcount=0),
            name="acme/widgets",
            default_branch="main",
            head_sha="sha1",
            items=items,
        )
