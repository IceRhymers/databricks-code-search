"""Integration tests for indexer.store's chunk_writer param against a Lakebase branch.

Reuses test_store.py's throwaway-schema fixture style, extended with a ``chunks``
table matching app.db.semantic's shape (``vector`` column via ``lakebase_vector``,
plain ``ts`` -- the generated column and BM25 behavior are test_semantic_rrf.py's
concern). Built with raw DDL (like test_semantic_rrf.py's fixture)
rather than ``semantic_metadata.create_all``: ``chunks.file_id`` references
``files.id`` across two separate ``MetaData`` instances (deliberately -- see
app/db/semantic.py), which SQLAlchemy's cross-metadata FK sorter can't resolve.

Proves the NEW issue #14 Phase 2 seam end-to-end: chunks written via a stub
chunk_writer ride the same conn.begin() as the rest of that file's row, and
cascade-delete when the file is swept (FK ON DELETE CASCADE), exactly like
symbols.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text

from app.config import SEMANTIC_EMBEDDING_DIM
from app.db.client import create_db_engine
from app.db.models import Base
from indexer.chunk_store import write_chunks
from indexer.languages import ExtractedSymbol, IndexCounts, ParsedFile
from indexer.store import index_repo

SCHEMA = "test_store_chunk_writer"


@pytest.fixture
def conn() -> Iterator[Connection]:
    engine = create_db_engine()
    connection = engine.connect()
    try:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS lakebase_vector"))
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
        # Extensions are database-wide and migration-owned; teardown drops only the schema.
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.commit()
        connection.close()
        engine.dispose()


def _pf(path: str, content: str) -> ParsedFile:
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


_STUB_VECTOR = [0.1] * SEMANTIC_EMBEDDING_DIM


def _stub_chunk_writer(conn: Connection, repo_id: int, file_id: int, pf: ParsedFile) -> None:
    # A fixed, precomputed 1-chunk-per-file "embedding" -- proves the seam without
    # needing a real embedder (issue #14 A4: chunk_writer never calls one).
    write_chunks(conn, file_id=file_id, chunks=[(0, pf.content, 1, 2, _STUB_VECTOR)])


def _count(conn: Connection, table: str, where: str = "") -> int:
    sql = f"SELECT count(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(text(sql)).scalar_one())


MAIN = ("main.py", "def f():\n    return 1\n", [ExtractedSymbol("f", "function", 1, 2)])
UTIL = ("util.py", "def g():\n    return 2\n", [ExtractedSymbol("g", "function", 1, 2)])


@pytest.mark.integration
def test_chunk_writer_none_is_byte_identical_to_the_core_path(conn: Connection) -> None:
    items = [(_pf(path, content), syms) for path, content, syms in (MAIN, UTIL)]
    counts = index_repo(
        conn, name="acme/widgets", branch="main", is_default=True, head_sha="sha_first", items=items
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0)
    assert _count(conn, "files") == 2
    assert _count(conn, "symbols") == 2
    assert _count(conn, "chunks") == 0  # no chunk_writer -> chunks untouched


@pytest.mark.integration
def test_chunk_writer_writes_inside_the_transaction(conn: Connection) -> None:
    items = [(_pf(path, content), syms) for path, content, syms in (MAIN, UTIL)]
    counts = index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_first",
        items=items,
        chunk_writer=_stub_chunk_writer,
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0)
    assert _count(conn, "chunks") == 2

    main_file_id = conn.execute(text("SELECT id FROM files WHERE path = 'main.py'")).scalar_one()
    assert _count(conn, "chunks", f"file_id = {main_file_id}") == 1


@pytest.mark.integration
def test_reindex_is_idempotent_for_chunks(conn: Connection) -> None:
    items = [(_pf(path, content), syms) for path, content, syms in (MAIN, UTIL)]
    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_first",
        items=items,
        chunk_writer=_stub_chunk_writer,
    )
    conn.rollback()
    counts = index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_first",
        items=items,
        chunk_writer=_stub_chunk_writer,
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0)
    assert _count(conn, "chunks") == 2  # delete-and-reinsert, not duplicated


@pytest.mark.integration
def test_chunks_cascade_delete_when_file_is_swept(conn: Connection) -> None:
    items = [(_pf(path, content), syms) for path, content, syms in (MAIN, UTIL)]
    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_first",
        items=items,
        chunk_writer=_stub_chunk_writer,
    )
    util_file_id = conn.execute(text("SELECT id FROM files WHERE path = 'util.py'")).scalar_one()
    assert _count(conn, "chunks", f"file_id = {util_file_id}") == 1
    conn.rollback()

    # Re-index without util.py at a new SHA -> util.py (and its chunks) swept.
    main_only = [(_pf(*MAIN[:2]), MAIN[2])]
    counts = index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_second",
        items=main_only,
        chunk_writer=_stub_chunk_writer,
    )
    assert counts == IndexCounts(files=1, symbols=1, swept=1)
    assert _count(conn, "files", "path = 'util.py'") == 0
    assert _count(conn, "chunks", f"file_id = {util_file_id}") == 0  # cascade
    assert _count(conn, "chunks") == 1
