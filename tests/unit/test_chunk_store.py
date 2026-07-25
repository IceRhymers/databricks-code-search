"""Unit tests for indexer.chunk_store.write_chunks(_batch): statement shape, no DB required.

A fake ``Connection`` records the statements/params passed to ``execute`` so the
delete-then-insert shape (and the absence of any embedding call) can be asserted
without a real Postgres. DB-touching coverage (actual insert + cascade behavior)
lives in tests/integration.
"""

from __future__ import annotations

from typing import Any

import pytest

from indexer.chunk_store import write_chunks, write_chunks_batch


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    def execute(self, stmt: Any, params: Any = None) -> Any:
        self.calls.append((stmt, params))
        return None


@pytest.mark.unit
def test_deletes_by_file_id_then_inserts_all_rows() -> None:
    conn = _FakeConn()
    written = write_chunks(
        conn,
        file_id=7,
        chunks=[(0, "chunk a", 1, 3, [0.1, 0.2]), (1, "chunk b", 4, 9, [0.3, 0.4])],
    )
    assert written == 2
    assert len(conn.calls) == 2

    delete_stmt, delete_params = conn.calls[0]
    assert delete_stmt.table.name == "chunks"
    assert delete_params == {"ids": [7]}

    insert_stmt, values = conn.calls[1]
    assert insert_stmt.table.name == "chunks"
    assert values is None  # rows travel inside stmt.values(...), not as a param list
    assert insert_stmt._multi_values[0] == [
        {
            "file_id": 7,
            "chunk_index": 0,
            "content": "chunk a",
            "start_line": 1,
            "end_line": 3,
            "embedding": [0.1, 0.2],
        },
        {
            "file_id": 7,
            "chunk_index": 1,
            "content": "chunk b",
            "start_line": 4,
            "end_line": 9,
            "embedding": [0.3, 0.4],
        },
    ]


@pytest.mark.unit
def test_ts_column_is_never_written() -> None:
    conn = _FakeConn()
    write_chunks(conn, file_id=1, chunks=[(0, "x", 1, 1, [0.0])])
    insert_stmt, _values = conn.calls[1]
    assert "ts" not in insert_stmt._multi_values[0][0]


@pytest.mark.unit
def test_empty_chunks_only_deletes() -> None:
    conn = _FakeConn()
    written = write_chunks(conn, file_id=3, chunks=[])
    assert written == 0
    assert len(conn.calls) == 1
    delete_stmt, _ = conn.calls[0]
    assert delete_stmt.table.name == "chunks"


@pytest.mark.unit
def test_never_touches_an_embedder() -> None:
    # write_chunks receives precomputed vectors only: nothing in
    # this module should reference app.embed at all.
    import indexer.chunk_store as chunk_store_module

    assert "embed" not in vars(chunk_store_module)


@pytest.mark.unit
def test_write_chunks_batch_empty_rows_is_a_zero_statement_no_op() -> None:
    conn = _FakeConn()
    written = write_chunks_batch(conn, rows=[])
    assert written == 0
    assert conn.calls == []


@pytest.mark.unit
def test_write_chunks_batch_deletes_every_id_including_zero_chunk_files() -> None:
    conn = _FakeConn()
    written = write_chunks_batch(
        conn,
        rows=[
            (7, [(0, "a", 1, 1, [0.1])]),
            (8, []),  # zero-chunk file -- still owed the delete
            (9, [(0, "b", 2, 2, [0.2]), (1, "c", 3, 3, [0.3])]),
        ],
    )
    assert written == 3
    assert len(conn.calls) == 2

    delete_stmt, delete_params = conn.calls[0]
    assert delete_stmt.table.name == "chunks"
    assert delete_params == {"ids": [7, 8, 9]}

    insert_stmt, _ = conn.calls[1]
    file_ids = [row["file_id"] for row in insert_stmt._multi_values[0]]
    assert file_ids == [7, 9, 9]  # file 8 contributes no insert rows


@pytest.mark.unit
def test_write_chunks_and_write_chunks_batch_issue_identical_statements() -> None:
    conn_a = _FakeConn()
    write_chunks(conn_a, file_id=42, chunks=[(0, "x", 1, 1, [0.0])])

    conn_b = _FakeConn()
    write_chunks_batch(conn_b, rows=[(42, [(0, "x", 1, 1, [0.0])])])

    assert len(conn_a.calls) == len(conn_b.calls) == 2
    assert conn_a.calls[0][1] == conn_b.calls[0][1]
    assert conn_a.calls[1][0]._multi_values[0] == conn_b.calls[1][0]._multi_values[0]
