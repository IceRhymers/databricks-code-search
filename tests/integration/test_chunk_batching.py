"""Integration tests for indexer.chunk_store.write_chunks_batch (#105) against real Postgres.

**Deliberately builds ``chunks`` on the BARE ``vector`` extension**, not
``lakebase_vector``: `codesearch-pg` (this repo's local dev Postgres) has
``vector 0.8.5`` installed, and ``lakebase_vector`` depends on that same base
extension (``app/alembic/versions/0004_semantic_chunks.py``). What actually
fails locally is ``CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE``
(the beta extension itself, unavailable outside Lakebase) --
``tests/integration/test_store_chunk_writer.py``'s whole module is
Lakebase-deferred for exactly that reason. This module's DDL diverges from
production in two ways -- a plain nullable ``ts tsvector`` (not a
``GENERATED`` column) and no ANN index -- and that divergence is IRRELEVANT to
what this module proves: ``write_chunks_batch``'s delete-by-``ANY`` and
multi-row insert semantics depend on the ``embedding`` column's TYPE, not on
whether an index exists over it or on how ``ts`` is populated. A separate
module from ``tests/integration/test_store_batching.py`` on purpose, so a
failure in this bare-``vector`` fixture can never take that module's coverage
down with it.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from sqlalchemy import Connection, text

from app.config import SEMANTIC_EMBEDDING_DIM
from app.db.client import create_db_engine
from app.db.models import Base
from indexer.chunk_store import write_chunks_batch
from indexer.languages import ExtractedSymbol, FileExtraction, ParsedFile
from indexer.store import index_repo

SCHEMA = "test_chunk_batching"


@pytest.fixture
def conn() -> Iterator[Connection]:
    engine = create_db_engine()
    connection = engine.connect()
    try:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.commit()
        connection.close()
        engine.dispose()


_STUB_VECTOR = [0.1] * SEMANTIC_EMBEDDING_DIM


def _seed_repo(conn: Connection, name: str = "acme/widgets") -> int:
    return int(
        conn.execute(
            text("INSERT INTO repos (name) VALUES (:name) RETURNING id"), {"name": name}
        ).scalar_one()
    )


def _seed_file(conn: Connection, repo_id: int, path: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO files "
                "(repo_id, path, lang, size, content, commit, content_sha, branches) "
                "VALUES (:repo_id, :path, 'python', 1, 'x', 'sha', :sha, ARRAY['main']) "
                "RETURNING id"
            ),
            {"repo_id": repo_id, "path": path, "sha": path},
        ).scalar_one()
    )


def _chunk_count(conn: Connection, file_id: int) -> int:
    return int(
        conn.execute(
            text("SELECT count(*) FROM chunks WHERE file_id = :id"), {"id": file_id}
        ).scalar_one()
    )


def _pf(path: str, content: str) -> ParsedFile:
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


def _items(
    *specs: tuple[str, str, list[ExtractedSymbol]],
) -> list[tuple[ParsedFile, FileExtraction]]:
    return [
        (_pf(path, content), FileExtraction(symbols=syms, edges=[]))
        for path, content, syms in specs
    ]


MAIN = ("main.py", "def f():\n    return 1\n", [ExtractedSymbol("f", "function", 1, 2)])
UTIL = ("util.py", "def g():\n    return 2\n", [ExtractedSymbol("g", "function", 1, 2)])


def _stub_chunk_writer(
    conn: Connection, repo_id: int, pairs: Sequence[tuple[int, ParsedFile]]
) -> None:
    write_chunks_batch(
        conn,
        rows=[(file_id, [(0, pf.content, 1, 2, _STUB_VECTOR)]) for file_id, pf in pairs],
    )


# --- Test 20: one delete + one insert per batch; zero-chunk covered files ----
# still deleted; uncovered paths untouched -----------------------------------


@pytest.mark.integration
def test_batch_write_deletes_zero_chunk_covered_files_and_leaves_uncovered_untouched(
    conn: Connection,
) -> None:
    repo_id = _seed_repo(conn)
    covered_a = _seed_file(conn, repo_id, "a.py")
    covered_b = _seed_file(conn, repo_id, "b.py")  # covered this run, but zero NEW chunks
    uncovered = _seed_file(conn, repo_id, "c.py")  # not part of this batch at all
    conn.commit()

    # Seed b.py and c.py with a prior chunk row each.
    write_chunks_batch(conn, rows=[(covered_b, [(0, "old", 1, 1, _STUB_VECTOR)])])
    write_chunks_batch(conn, rows=[(uncovered, [(0, "old", 1, 1, _STUB_VECTOR)])])
    conn.commit()

    written = write_chunks_batch(
        conn,
        rows=[
            (covered_a, [(0, "a chunk", 1, 1, _STUB_VECTOR)]),
            (covered_b, []),
        ],
    )
    assert written == 1

    assert _chunk_count(conn, covered_a) == 1
    assert _chunk_count(conn, covered_b) == 0  # deleted, not left stale
    assert _chunk_count(conn, uncovered) == 1  # untouched -- not in this batch


# --- Test 21: FK cascade still removes chunks when the sweep deletes a file -


# --- membership-only + real chunks: the case tests/integration/test_store_chunk_writer.py
# cannot exercise locally (Lakebase-deferred), covered here since this module's
# bare-`vector` fixture actually runs against codesearch-pg. -----------------


@pytest.mark.integration
def test_membership_only_acquisition_writes_chunks_for_the_acquiring_branch(
    conn: Connection,
) -> None:
    """Branch 'b' acquires MAIN's content already stored under branch 'a' via
    the membership-only path (``indexer.store._union_membership``) -- no
    symbol/edge rewrite, but chunks ARE written for the acquiring branch via
    the same ``write_chunks_batch`` this module otherwise tests.
    """
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a1",
        items=_items(MAIN),
        chunk_writer=_stub_chunk_writer,
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
    conn.rollback()

    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b2",
        items=_items(MAIN, UTIL),
        chunk_writer=_stub_chunk_writer,
    )
    main_file_id = conn.execute(text("SELECT id FROM files WHERE path = 'main.py'")).scalar_one()
    assert _chunk_count(conn, main_file_id) == 1


@pytest.mark.integration
def test_membership_only_duplicate_item_does_not_poison_the_chunk_insert(
    conn: Connection,
) -> None:
    """A duplicated ``(path, content_sha)`` entry in ``items`` landing in the
    membership-only class must not raise a UNIQUE VIOLATION against
    ``uq_chunks_file_id_chunk_index``. ``write_chunks_batch``'s dedup guard
    (mirroring ``indexer.store._flush_file_batch``'s own guard) is what
    prevents it -- without it, ``_union_membership`` would hand the same
    ``file_id`` to ``chunk_writer`` twice, and the batch insert would attempt
    two rows with the same ``(file_id, chunk_index)``.

    ``items`` is an injected seam (this module, ``tests/integration/test_reconcile.py``,
    and any other direct caller can feed it a duplicate) even though the
    production source (``indexer/ingest.py``'s ``iter_tar_source_files``) never
    does.
    """
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a1",
        items=_items(MAIN),
        chunk_writer=_stub_chunk_writer,
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
    conn.rollback()

    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b2",
        items=_items(MAIN, MAIN, UTIL),
        chunk_writer=_stub_chunk_writer,
    )
    main_file_id = conn.execute(text("SELECT id FROM files WHERE path = 'main.py'")).scalar_one()
    assert _chunk_count(conn, main_file_id) == 1


@pytest.mark.integration
def test_fk_cascade_removes_chunk_rows_when_sweep_deletes_an_emptied_file(
    conn: Connection,
) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha1",
        items=_items(MAIN, UTIL),
        chunk_writer=_stub_chunk_writer,
    )
    util_file_id = conn.execute(text("SELECT id FROM files WHERE path = 'util.py'")).scalar_one()
    assert _chunk_count(conn, util_file_id) == 1
    conn.rollback()

    counts = index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha2",
        items=_items(MAIN),  # util.py dropped -> swept
        chunk_writer=_stub_chunk_writer,
    )
    assert counts.swept == 1
    assert conn.execute(text("SELECT count(*) FROM files WHERE path = 'util.py'")).scalar_one() == 0
    assert _chunk_count(conn, util_file_id) == 0  # FK ON DELETE CASCADE
