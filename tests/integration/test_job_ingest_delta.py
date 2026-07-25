"""Streaming ingestion does not rewrite an already-indexed corpus (#106 AC3 + #104).

This is the end-to-end proof that #106's byte-identical-corpus claim survives
contact with #104's file-level delta gate. AC3 forbids an
``INDEX_SEMANTICS_VERSION`` bump, so the delta gate is **open** on the first run
after #106 deploys -- which means every file whose ``(path, content_sha)`` still
matches issues no statement at all. If ``ingest.py`` diverged from ``parse.py`` in
any content-affecting way, that first run would instead reclassify the whole
corpus as changed and rewrite it.

The ``lang``/``size`` half of that guarantee is NOT observable here and must not
be assumed from a green run: the gate keys on ``(path, content_sha)`` alone, so a
``lang``/``size`` divergence would ALSO show up as all-unchanged, silently and
permanently. That dimension is pinned by the oracle assertions in
``tests/unit/test_ingest_parity.py``.

Clones ``tests/integration/test_store_delta.py``'s throwaway-schema fixture idiom
(own copy, per ``tests/integration/AGENTS.md``'s no-conftest convention), and for
the same reason builds no ``chunks`` table and creates no ``lakebase_*``
extension -- that is what lets it run against a plain local Postgres.
"""

from __future__ import annotations

import logging
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, text

from app.db.client import create_db_engine
from app.db.models import Base
from indexer.ingest import iter_tar_source_files
from indexer.languages import IndexCounts
from indexer.parse import iter_source_files
from indexer.store import index_repo
from indexer.symbols import extract_file
from tests.unit.test_ingest import TOP, _dir, _Entry, _reg, _write

SCHEMA = "test_job_ingest_delta"
REPO = "acme/widgets"

# Deliberately mixed: mapped and unmapped extensions, a nested directory, and two
# members BOTH paths must skip -- so an "all unchanged" result also proves the two
# implementations agree on what is NOT in the corpus.
_FIXTURE: list[_Entry] = [
    _dir(TOP),
    _reg(f"{TOP}/main.py", b"def f():\n    return 1\n"),
    _reg(f"{TOP}/pkg/util.py", b"class C:\n    pass\n"),
    _reg(f"{TOP}/app.js", b"function g() { return 2; }\n"),
    _reg(f"{TOP}/README.md", b"# hello\n"),
    _reg(f"{TOP}/.git/config", b"[core]\n"),
    _reg(f"{TOP}/logo.png", b"\x89PNG\x00\x00binary"),
]


@pytest.fixture
def conn() -> Iterator[Connection]:
    engine = create_db_engine()
    connection = engine.connect()
    try:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        connection.execute(text(f"SET search_path TO {SCHEMA}, public"))
        connection.commit()

        Base.metadata.create_all(bind=connection)
        connection.commit()

        yield connection
    finally:
        connection.rollback()
        connection.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        connection.commit()
        connection.close()
        engine.dispose()


def _delta_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "indexer.store" and "delta write set" in r.getMessage()
    ]


@pytest.mark.integration
def test_reindexing_an_already_indexed_branch_is_all_unchanged(
    conn: Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Index through the OLD path, re-index the SAME tarball through the new one.

    Leg 1 extracts to disk and walks with ``parse.iter_source_files``, exactly as
    ``extract_tarball`` + the pre-#106 job did. Leg 2 streams the same archive
    with ``iter_tar_source_files`` at a new ``head_sha``. Every file must classify
    as *unchanged*: zero writes, and every ``files.id``/``symbols.id`` preserved
    (a delete-reinsert would renumber the serials, so identical ids are the
    precise proof).
    """
    tar_path = _write(tmp_path, _FIXTURE)

    dest = tmp_path / "extracted"
    dest.mkdir()
    with tarfile.open(tar_path, mode="r:*") as tf:
        tf.extractall(dest, filter="data")
    old_items = [(pf, extract_file(pf)) for pf in iter_source_files(dest / TOP)]

    first = index_repo(
        conn,
        name=REPO,
        branch="main",
        is_default=True,
        head_sha="sha_first",
        items=old_items,
    )
    assert first.files == len(old_items) == 4  # .git/config and the PNG are skipped
    assert first.symbols > 0

    files_before = dict(conn.execute(text("SELECT path, id FROM files ORDER BY path")).all())
    symbols_before = sorted(conn.execute(text("SELECT id FROM symbols")).scalars().all())
    conn.rollback()

    with caplog.at_level(logging.INFO, logger="indexer.store"):
        second = index_repo(
            conn,
            name=REPO,
            branch="main",
            is_default=True,
            head_sha="sha_second",
            items=((pf, extract_file(pf)) for pf in iter_tar_source_files(tar_path)),
        )

    assert second == IndexCounts(files=first.files, symbols=0, swept=0, edges=0)

    files_after = dict(conn.execute(text("SELECT path, id FROM files ORDER BY path")).all())
    symbols_after = sorted(conn.execute(text("SELECT id FROM symbols")).scalars().all())
    assert files_after == files_before
    assert symbols_after == symbols_before

    lines = _delta_lines(caplog)
    assert len(lines) == 1
    assert (
        f"delta write set 0/{first.files} files "
        f"(unchanged={first.files} membership=0, semantics gate open)" in lines[0]
    )
