"""Write PRECOMPUTED chunk+embedding rows for one file into ``chunks`` (issue #14).

Mirrors ``indexer.store``'s connection seam: the caller supplies a live
``sqlalchemy.Connection`` and owns the transaction (``conn.begin()``); this
module never opens its own engine. Like ``index_repo``'s symbol handling,
chunks carry no natural key, so a re-index deletes ``file_id``'s existing rows
and reinserts the current set -- idempotent, and safe to call repeatedly
within the same per-file loop.

This module never calls the embedder: ``chunks`` arrives with vectors already
computed by :mod:`indexer.embed`, so writing them is pure DML with no network
call inside the caller's lock window (issue #14 A4). ``ts`` is a ``GENERATED``
column in production (backed by the beta ``lakebase_text`` extension) and is
therefore never written here -- it derives from ``content``.

Note: unlike ``symbols``, the current ``app.db.semantic.chunks`` schema has no
``repo_id`` column (chunks are scoped by ``file_id`` only, joining to
``files.repo_id`` if a repo-scoped semantic query ever needs it), so this
seam takes no ``repo_id`` parameter.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Connection, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.semantic import chunks as chunks_table


def write_chunks(
    conn: Connection,
    *,
    file_id: int,
    chunks: Sequence[tuple[int, str, list[float]]],
) -> int:
    """Delete-and-reinsert ``file_id``'s chunk rows; return the row count written.

    ``chunks`` is a sequence of ``(chunk_index, content, embedding)`` triples
    with embeddings already computed. Runs inside the caller's open
    transaction, alongside the rest of that file's ``index_repo`` work.
    """
    conn.execute(delete(chunks_table).where(chunks_table.c.file_id == file_id))
    if not chunks:
        return 0

    conn.execute(
        pg_insert(chunks_table),
        [
            {
                "file_id": file_id,
                "chunk_index": chunk_index,
                "content": content,
                "embedding": embedding,
            }
            for chunk_index, content, embedding in chunks
        ],
    )
    return len(chunks)
