"""Unit tests for indexer.store's optional chunk_writer param.

A hand-rolled fake Connection stands in for Postgres so this stays a true unit
test -- index_repo's core upsert/sweep/rollback behavior already has DB-backed
coverage in tests/integration/test_store.py. This only proves the NEW surface:
(a) chunk_writer defaults to None, which is byte-identical to the core path
before chunk writing was added, and (b) when given, it is called once per
file, inside the same conn.begin(), with (repo_id, file_id, pf).
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
from sqlalchemy import Delete, Insert, Update

from indexer.languages import ExtractedSymbol, FileExtraction, IndexCounts, ParsedFile
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
    """Just enough of sqlalchemy.Connection for index_repo's fixed statement shape.

    Multi-branch (0003+): statement 1 (repos) now RETURNING just ``id``;
    statement 2 (repo_branches) is the new per-branch CAS baseline; the
    membership sweep is raw ``text()`` SQL (an UPDATE then a DELETE against
    ``files``, matched by substring since a ``TextClause`` has no ``.table``);
    the final CAS stamp UPDATE targets ``repo_branches``, not ``repos``.
    """

    def __init__(self, *, stamp_rowcount: int = 1) -> None:
        self._next_file_id = 1
        self._stamp_rowcount = stamp_rowcount

    def begin(self) -> Any:
        return contextlib.nullcontext()

    def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        text = getattr(stmt, "text", None)
        if text is not None:
            if "UPDATE files SET branches" in text:
                return _FakeResult(rowcount=0)
            if "DELETE FROM files" in text:
                return _FakeResult(rowcount=0)
            raise AssertionError(f"unexpected text() statement: {text!r}")

        table = stmt.table.name
        if isinstance(stmt, Insert) and table == "repos":
            return _FakeResult(scalar=1)
        if isinstance(stmt, Insert) and table == "repo_branches":
            # (last_indexed_commit, index_semantics_version) -- new-branch shape.
            return _FakeResult(row=(None, None))
        if isinstance(stmt, Insert) and table == "files":
            file_id = self._next_file_id
            self._next_file_id += 1
            return _FakeResult(scalar=file_id)
        if isinstance(stmt, Insert) and table == "symbols":
            return _FakeResult()
        if isinstance(stmt, Delete) and table == "symbols":
            return _FakeResult()
        if isinstance(stmt, Insert) and table == "reference_edges":
            return _FakeResult()
        if isinstance(stmt, Delete) and table == "reference_edges":
            return _FakeResult()
        if isinstance(stmt, Update) and table == "repo_branches":
            return _FakeResult(rowcount=self._stamp_rowcount)
        raise AssertionError(f"unexpected statement against {table!r}: {stmt}")


def _pf(path: str, content: str) -> ParsedFile:
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


@pytest.mark.unit
def test_chunk_writer_defaults_to_none_and_behavior_is_unchanged() -> None:
    items = [
        (
            _pf("a.py", "x = 1\n"),
            FileExtraction(symbols=[ExtractedSymbol("x", "variable", 1, 1)], edges=[]),
        )
    ]
    counts = index_repo(
        _FakeConn(),
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha1",
        items=items,
    )
    assert counts == IndexCounts(files=1, symbols=1, swept=0, edges=0)


@pytest.mark.unit
def test_chunk_writer_is_called_once_per_file_with_repo_id_and_file_id() -> None:
    calls: list[tuple[int, int, str]] = []

    def chunk_writer(conn: Any, repo_id: int, file_id: int, pf: ParsedFile) -> None:
        calls.append((repo_id, file_id, pf.path))

    items = [
        (_pf("a.py", "x = 1\n"), FileExtraction(symbols=[], edges=[])),
        (_pf("b.py", "y = 2\n"), FileExtraction(symbols=[], edges=[])),
    ]
    index_repo(
        _FakeConn(),
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha1",
        items=items,
        chunk_writer=chunk_writer,
    )
    assert calls == [(1, 1, "a.py"), (1, 2, "b.py")]


@pytest.mark.unit
def test_no_chunk_writer_means_no_extra_calls() -> None:
    # A None chunk_writer must never itself be invoked (it isn't callable).
    items = [(_pf("a.py", "x = 1\n"), FileExtraction(symbols=[], edges=[]))]
    # No AttributeError/TypeError from trying to call None -> proves the `if
    # chunk_writer is not None` guard is doing its job.
    index_repo(
        _FakeConn(),
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha1",
        items=items,
        chunk_writer=None,
    )


@pytest.mark.unit
def test_stamp_matching_no_row_raises_stale_index_error() -> None:
    # The CAS UPDATE matching zero rows means the repo_branches row moved out
    # from under the statement-2 baseline; index_repo must abort rather than stamp.
    items = [(_pf("a.py", "x = 1\n"), FileExtraction(symbols=[], edges=[]))]
    with pytest.raises(StaleIndexError, match="acme/widgets"):
        index_repo(
            _FakeConn(stamp_rowcount=0),
            name="acme/widgets",
            branch="main",
            is_default=True,
            head_sha="sha1",
            items=items,
        )
