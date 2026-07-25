"""Write PRECOMPUTED chunk+embedding rows for many files into ``chunks``.

Mirrors ``indexer.store``'s connection seam: the caller supplies a live
``sqlalchemy.Connection`` and owns the transaction (``conn.begin()``); this
module never opens its own engine. Like ``index_repo``'s symbol handling,
chunks carry no natural key, so a re-index deletes each file's existing rows
and reinserts the current set -- idempotent, and safe to call repeatedly
within the same per-batch flush.

This module never calls the embedder: ``chunks`` arrives with vectors already
computed by :mod:`app.embed`, so writing them is pure DML with no network
call inside the caller's lock window. ``ts`` is a ``GENERATED`` column in
production (backed by the beta ``lakebase_text`` extension) and is therefore
never written here -- it derives from ``content``.

Note: unlike ``symbols``, the current ``app.db.semantic.chunks`` schema has no
``repo_id`` column (chunks are scoped by ``file_id`` only, joining to
``files.repo_id`` if a repo-scoped semantic query ever needs it), so this
seam takes no ``repo_id`` parameter.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import ARRAY, BigInteger, Connection, any_, bindparam, delete

from app.db.semantic import chunks as chunks_table
from indexer.bulk import CHUNK_PARAM_BUDGET, insert_rows

# One (chunk_index, content, start_line, end_line, embedding) tuple per chunk,
# in the shape the line-aligned chunker produces.
ChunkRow = tuple[int, str, int, int, list[float]]


def write_chunks_batch(
    conn: Connection,
    *,
    rows: Sequence[tuple[int, Sequence[ChunkRow]]],
) -> int:
    """Delete-and-reinsert chunk rows for many files in one delete + one bulk insert.

    ``rows`` is a sequence of ``(file_id, chunks)`` pairs. A zero-statement
    no-op on an empty ``rows`` -- no ``conn`` call at all. Otherwise: one
    ``DELETE ... WHERE file_id = ANY(:ids)`` over EVERY file id in the call --
    including files with zero chunk rows, since delete-on-zero-chunks is
    load-bearing (a file that shrank to zero chunks must still lose its stale
    rows) -- then one param-budgeted bulk insert via :func:`indexer.bulk.insert_rows`.
    """
    if not rows:
        return 0

    ids = [file_id for file_id, _chunks in rows]
    conn.execute(
        delete(chunks_table).where(
            chunks_table.c.file_id == any_(bindparam("ids", type_=ARRAY(BigInteger)))
        ),
        {"ids": ids},
    )

    insert_dicts = [
        {
            "file_id": file_id,
            "chunk_index": chunk_index,
            "content": content,
            "start_line": start_line,
            "end_line": end_line,
            "embedding": embedding,
        }
        for file_id, chunks in rows
        for chunk_index, content, start_line, end_line, embedding in chunks
    ]
    insert_rows(conn, chunks_table, insert_dicts, param_budget=CHUNK_PARAM_BUDGET)
    return len(insert_dicts)


def write_chunks(
    conn: Connection,
    *,
    file_id: int,
    chunks: Sequence[ChunkRow],
) -> int:
    """Delete-and-reinsert ``file_id``'s chunk rows; return the row count written.

    A one-element wrapper over :func:`write_chunks_batch` -- one DML
    implementation, not two that can drift. Signature and return value are
    unchanged from before batching.
    """
    return write_chunks_batch(conn, rows=[(file_id, chunks)])
