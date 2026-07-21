"""Unit tests for indexer.chunk_store.write_chunks: statement shape, no DB required.

A fake ``Connection`` records the statements/params passed to ``execute`` so the
delete-then-insert shape (and the absence of any embedding call) can be asserted
without a real Postgres. DB-touching coverage (actual insert + cascade behavior)
lives in tests/integration.
"""

from __future__ import annotations

from typing import Any

import pytest

from indexer.chunk_store import write_chunks


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
    assert delete_params is None

    insert_stmt, values = conn.calls[1]
    assert insert_stmt.table.name == "chunks"
    assert values == [
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
    _insert_stmt, values = conn.calls[1]
    assert "ts" not in values[0]


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
    # write_chunks receives precomputed vectors only (issue #14 A4): nothing in
    # this module should reference indexer.embed at all.
    import indexer.chunk_store as chunk_store_module

    assert "embed" not in vars(chunk_store_module)
