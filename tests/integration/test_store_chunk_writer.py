"""Integration tests for indexer.store's chunk_writer param against a Lakebase branch.

Reuses test_store.py's throwaway-schema fixture style, extended with a ``chunks``
table matching app.db.semantic's shape (``vector`` column via ``lakebase_vector``,
plain ``ts`` -- the generated column and BM25 behavior are test_semantic_rrf.py's
concern). Built with raw DDL (like test_semantic_rrf.py's fixture)
rather than ``semantic_metadata.create_all``: ``chunks.file_id`` references
``files.id`` across two separate ``MetaData`` instances (deliberately -- see
app/db/semantic.py), which SQLAlchemy's cross-metadata FK sorter can't resolve.

Proves the chunk_writer seam end-to-end: chunks written via a stub
chunk_writer ride the same conn.begin() as the rest of that file's row, and
cascade-delete when the file is swept (FK ON DELETE CASCADE), exactly like
symbols.

**Every test in this module is Lakebase-deferred for issue #104**: this is the
one module whose fixture builds the ``chunks`` table and needs
``lakebase_vector``, which no local Postgres image provides (see
``tests/integration/test_store_delta.py``'s module docstring for why the
core delta suite deliberately lives elsewhere instead). The delta-specific
additions here (the unchanged-file / membership-only chunk cases, and
``test_reindex_is_idempotent_for_chunks``'s reviewed expectations) are
reasoned through against ``indexer/store.py``'s ``_union_membership`` and
the per-file loop, never verified by a local run -- flagged explicitly in the
PR body, per the plan for issue #104, §2.6a / §3.2.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, text

from app.config import SEMANTIC_EMBEDDING_DIM
from app.db.client import create_db_engine
from app.db.models import Base
from indexer.chunk_store import write_chunks
from indexer.languages import ExtractedSymbol, FileExtraction, IndexCounts, ParsedFile
from indexer.store import index_repo

SCHEMA = "test_store_chunk_writer"


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
    # needing a real embedder (chunk_writer never calls one).
    write_chunks(conn, file_id=file_id, chunks=[(0, pf.content, 1, 2, _STUB_VECTOR)])


def _count(conn: Connection, table: str, where: str = "") -> int:
    sql = f"SELECT count(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(text(sql)).scalar_one())


MAIN = ("main.py", "def f():\n    return 1\n", [ExtractedSymbol("f", "function", 1, 2)])
UTIL = ("util.py", "def g():\n    return 2\n", [ExtractedSymbol("g", "function", 1, 2)])


def _items(
    *specs: tuple[str, str, list[ExtractedSymbol]],
) -> list[tuple[ParsedFile, FileExtraction]]:
    return [
        (_pf(path, content), FileExtraction(symbols=syms, edges=[]))
        for path, content, syms in specs
    ]


@pytest.mark.integration
def test_chunk_writer_none_is_byte_identical_to_the_core_path(conn: Connection) -> None:
    items = _items(MAIN, UTIL)
    counts = index_repo(
        conn, name="acme/widgets", branch="main", is_default=True, head_sha="sha_first", items=items
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0, edges=0)
    assert _count(conn, "files") == 2
    assert _count(conn, "symbols") == 2
    assert _count(conn, "chunks") == 0  # no chunk_writer -> chunks untouched


@pytest.mark.integration
def test_chunk_writer_writes_inside_the_transaction(conn: Connection) -> None:
    items = _items(MAIN, UTIL)
    counts = index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_first",
        items=items,
        chunk_writer=_stub_chunk_writer,
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0, edges=0)
    assert _count(conn, "chunks") == 2

    main_file_id = conn.execute(text("SELECT id FROM files WHERE path = 'main.py'")).scalar_one()
    assert _count(conn, "chunks", f"file_id = {main_file_id}") == 1


@pytest.mark.integration
def test_reindex_is_idempotent_for_chunks(conn: Connection) -> None:
    """Reviewed against the plan for issue #104, §2.6a's treatment table (Lakebase-
    deferred, so this review is reasoned through rather than locally verified --
    see the module docstring). Unlike the two GUARD tests in
    ``tests/integration/test_store.py`` (which deliberately hold content_sha
    identical while varying EXTRACTION output, and must keep their gate forced
    closed), this run is identical in every respect -- content, head_sha, AND
    extraction. The first run stamps INDEX_SEMANTICS_VERSION, so the second
    classifies both files unchanged: ``symbols`` drops from 2 (a delete-reinsert
    count) to 0 (nothing rewritten), a plain (a)-style value update. The chunk
    row COUNT is unaffected either way -- 2 rows survive whether by an idempotent
    delete-reinsert (pre-#104) or by never being touched at all (unchanged, under
    #104) -- so that assertion needed no change, only its reasoning.
    """
    items = _items(MAIN, UTIL)
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
    assert counts == IndexCounts(files=2, symbols=0, swept=0, edges=0)
    assert _count(conn, "chunks") == 2  # untouched, not delete-and-reinserted


@pytest.mark.integration
def test_chunks_cascade_delete_when_file_is_swept(conn: Connection) -> None:
    items = _items(MAIN, UTIL)
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
    main_only = _items(MAIN)
    counts = index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_second",
        items=main_only,
        chunk_writer=_stub_chunk_writer,
    )
    assert counts == IndexCounts(files=1, symbols=1, swept=1, edges=0)
    assert _count(conn, "files", "path = 'util.py'") == 0
    assert _count(conn, "chunks", f"file_id = {util_file_id}") == 0  # cascade
    assert _count(conn, "chunks") == 1


# --- File-level delta indexing (#104): unchanged / membership-only chunks ---
# Lakebase-deferred -- see the module docstring. Reasoned through against
# indexer/store.py's per-file loop and _union_membership, never locally run.


@pytest.mark.integration
def test_unchanged_file_never_calls_chunk_writer_and_preserves_chunk_ids(
    conn: Connection,
) -> None:
    """An unchanged file's chunks.id values are IDENTICAL before/after, and
    chunk_writer is never called for it at all -- proven with a call-tracking
    wrapper, not just by the row count staying flat (which an idempotent
    delete-reinsert of identical content would also produce, as
    test_reindex_is_idempotent_for_chunks above shows)."""
    calls: list[str] = []

    def _tracking_chunk_writer(
        conn: Connection, repo_id: int, file_id: int, pf: ParsedFile
    ) -> None:
        calls.append(pf.path)
        _stub_chunk_writer(conn, repo_id, file_id, pf)

    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_first",
        items=_items(MAIN, UTIL),
        chunk_writer=_tracking_chunk_writer,
    )
    chunk_ids_before = sorted(conn.execute(text("SELECT id FROM chunks")).scalars().all())
    conn.rollback()
    calls.clear()

    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_second",
        items=_items(MAIN, UTIL),
        chunk_writer=_tracking_chunk_writer,
    )
    assert calls == []
    chunk_ids_after = sorted(conn.execute(text("SELECT id FROM chunks")).scalars().all())
    assert chunk_ids_after == chunk_ids_before


@pytest.mark.integration
def test_membership_only_file_backfills_previously_missing_chunks(conn: Connection) -> None:
    """§2.4 note 3: a membership-acquired file whose row had ZERO chunk rows
    (branch 'a' wrote it with chunk_writer=None, i.e. semantic-off) ends branch
    'b's acquiring run WITH chunk rows. Membership-only writes chunks even
    though it writes no symbols/edges -- the vectors are already in hand
    (job.py embeds every file the advisory read did not call unchanged), so
    skipping the write would make a semantic-off-then-on transition's gap
    permanent for the acquiring branch instead of backfilling it."""
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a1",
        items=_items(MAIN),  # no chunk_writer -> main.py has zero chunk rows
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b1",
        items=_items(UTIL),
        chunk_writer=_stub_chunk_writer,
    )
    main_file_id = conn.execute(text("SELECT id FROM files WHERE path = 'main.py'")).scalar_one()
    assert _count(conn, "chunks", f"file_id = {main_file_id}") == 0
    conn.rollback()

    # branch 'b' is now at the current semantics version (its own baseline),
    # so acquiring main.py (identical content, still stored under 'a') takes
    # the membership-only path.
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b2",
        items=_items(MAIN, UTIL),
        chunk_writer=_stub_chunk_writer,
    )
    assert _count(conn, "chunks", f"file_id = {main_file_id}") == 1
    branches = conn.execute(text("SELECT branches FROM files WHERE path = 'main.py'")).scalar_one()
    assert sorted(branches) == ["a", "b"]
