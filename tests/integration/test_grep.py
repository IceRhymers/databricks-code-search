"""Integration tests for grep search (issue #10): query -> executed SQL -> line matches.

Requires a running Postgres with the standard PG* env set. Mirrors the throwaway-schema
idiom of ``tests/integration/test_query_compiler.py`` (unique schema, ``SET search_path``,
``CREATE EXTENSION pg_trgm``, ``Base.metadata.create_all`` -- which also builds the trgm
GIN indexes -- and ``DROP SCHEMA ... CASCADE`` + ``engine.dispose()`` in ``finally``). In
this repo that Postgres exists only as CI's service container, so these tests are CI-only
and were validated locally by lint/type-check + ``--collect-only``, not execution.

The ``seeded`` fixture is function-scoped: some tests pass a tiny ``statement_timeout_ms``
that a shared module-scoped connection could leak, and one test relies on ``running``
starting at 0, so each test gets a clean connection/corpus.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import NamedTuple

import pytest
from sqlalchemy import Connection, insert, text

from app.db.client import create_db_engine
from app.db.models import Base, File, Repo
from app.search.errors import QueryTooBroadError
from app.search.grep import GrepResult, grep_search

SCHEMA_PREFIX = "test_grep"


class Seeded(NamedTuple):
    conn: Connection
    acme_id: int
    beta_id: int


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _insert_repo(conn: Connection, name: str) -> int:
    return conn.execute(insert(Repo).values(name=name).returning(Repo.id)).scalar_one()


def _insert_file(
    conn: Connection,
    repo_id: int,
    path: str,
    *,
    lang: str | None,
    content: str | None,
) -> int:
    return conn.execute(
        insert(File)
        .values(repo_id=repo_id, path=path, lang=lang, content=content)
        .returning(File.id)
    ).scalar_one()


@pytest.fixture
def seeded() -> Iterator[Seeded]:
    """Throwaway schema + durable-core DDL + a small deterministic corpus."""
    schema = _unique(SCHEMA_PREFIX)
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.commit()

        Base.metadata.create_all(bind=conn)
        conn.commit()

        acme_id = _insert_repo(conn, "acme/widgets")
        beta_id = _insert_repo(conn, "beta/tools")

        _insert_file(
            conn,
            acme_id,
            "src/handler.go",
            lang="go",
            content="package main\nfunc Handler() {}\n// foo lives here and foo again\n",
        )
        _insert_file(
            conn,
            acme_id,
            "src/util.go",
            lang="go",
            content="// 你好 foo trailing multibyte line\nno match on this line\n",
        )
        _insert_file(
            conn,
            beta_id,
            "pkg/note.py",
            lang="python",
            content="# beta foo note\n# Foo capitalized here\n",
        )
        conn.commit()

        yield Seeded(conn=conn, acme_id=acme_id, beta_id=beta_id)
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


def _paths(result: GrepResult) -> list[str]:
    return [f.path for f in result.files]


# --------------------------------------------------------------------------- 1. grouping


@pytest.mark.integration
def test_grep_groups_matches_per_file_in_repo_id_path_order(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "foo")
    # Ordered by (repo_id, path): both acme files, then the beta file.
    assert _paths(result) == ["src/handler.go", "src/util.go", "pkg/note.py"]
    assert result.truncated is False
    assert result.truncation_reason is None

    handler = result.files[0]
    # "// foo lives here and foo again" is line 3, with two matches merged into two spans.
    (line,) = handler.line_matches
    assert line.line_number == 3
    assert len(line.byte_ranges) == 2
    for start, end in line.byte_ranges:
        assert line.line_text.encode("utf-8")[start:end] == b"foo"


@pytest.mark.integration
def test_grep_multibyte_byte_ranges_are_utf8_accurate(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "foo")
    util = next(f for f in result.files if f.path == "src/util.go")
    (line,) = util.line_matches
    assert line.line_number == 1
    (span,) = line.byte_ranges
    start, end = span
    assert line.line_text.encode("utf-8")[start:end] == b"foo"


# ------------------------------------------------------------------------- 2. case


@pytest.mark.integration
def test_grep_case_sensitivity_end_to_end(seeded: Seeded) -> None:
    # Case-insensitive "Foo" hits both the lowercase and capitalized beta lines.
    insensitive = grep_search(seeded.conn, "Foo")
    note = next(f for f in insensitive.files if f.path == "pkg/note.py")
    assert [m.line_number for m in note.line_matches] == [1, 2]

    # case:yes Foo only matches the capitalized "Foo" on line 2.
    sensitive = grep_search(seeded.conn, "case:yes Foo")
    assert _paths(sensitive) == ["pkg/note.py"]
    note_cs = sensitive.files[0]
    assert [m.line_number for m in note_cs.line_matches] == [2]


# ------------------------------------------------------------------------- 3. regex


@pytest.mark.integration
def test_grep_regex_end_to_end(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "/f.o/")
    assert set(_paths(result)) == {"src/handler.go", "src/util.go", "pkg/note.py"}
    assert result.regex_incompatible is False


# ------------------------------------------------------------------------- 4. and/or


@pytest.mark.integration
def test_grep_and_predicate_restricts_files_but_highlights_either_atom(seeded: Seeded) -> None:
    # "foo Handler" -> only src/handler.go passes the SQL AND predicate; its lines
    # highlight either content atom.
    result = grep_search(seeded.conn, "foo Handler")
    assert _paths(result) == ["src/handler.go"]
    handler = result.files[0]
    matched = {m.line_number for m in handler.line_matches}
    assert matched == {2, 3}  # line 2 "func Handler()", line 3 "foo ... foo"


# ------------------------------------------------------------------------- 5. filters


@pytest.mark.integration
def test_grep_lang_filter_restricts_candidates(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "lang:go foo")
    # Only the go files are candidates; the python beta file is excluded.
    assert _paths(result) == ["src/handler.go", "src/util.go"]


# ------------------------------------------------------- 6. statement_timeout -> raise


@pytest.mark.integration
def test_tiny_statement_timeout_raises_query_too_broad(seeded: Seeded) -> None:
    # Deterministic DB-cancellation by WORK VOLUME, not regex pathology. Postgres's regex
    # engine is a Spencer NFA hybrid, not a naive backtracker -- classic ReDoS patterns like
    # ``(a+)+$`` fail fast, so they cannot be relied on to exceed a tiny timeout. Instead we
    # force a full sequential scan whose regex recheck runs over ~16 MB of content: a 2-char
    # pattern is too short for pg_trgm to index (no trigrams) so the GIN index cannot exclude
    # rows, and it matches nothing so LIMIT never short-circuits. That guarantees >> 1 ms of
    # work; Postgres's regex engine honors CHECK_FOR_INTERRUPTS, so statement_timeout cancels
    # the candidate query itself. (Scope: this covers the DB-cancellation path only; the
    # Python-rescan CPU gap is a documented, untested V1 limitation -- see grep.py.)
    blob = "a" * (2 * 1024 * 1024)  # 2 MiB per file, 8 files -> ~16 MiB scanned
    for i in range(8):
        _insert_file(seeded.conn, seeded.acme_id, f"src/blob{i}.txt", lang=None, content=blob)
    seeded.conn.commit()
    with pytest.raises(QueryTooBroadError):
        grep_search(seeded.conn, "/zq/", statement_timeout_ms=1)


@pytest.mark.integration
def test_healthy_query_does_not_raise_query_too_broad(seeded: Seeded) -> None:
    # Positive control: a DB-fast, non-pathological regex under the default timeout
    # completes and returns matches -- the timeout guard must not fire spuriously.
    result = grep_search(seeded.conn, "/f.o/")
    assert result.files
    assert result.truncated is False


# ----------------------------------------------------------------- 7. byte cap -> truncated


@pytest.mark.integration
def test_tiny_byte_cap_truncates_result(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "foo", max_content_bytes=10)
    assert result.truncated is True
    assert result.truncation_reason == "byte_cap"


@pytest.mark.integration
def test_single_over_cap_file_is_omitted_not_partially_returned(seeded: Seeded) -> None:
    # A schema with a single matching file whose own content exceeds the cap: because
    # the cap is checked before processing and `running` starts at 0, the sole file
    # trips the cap and is skipped -> zero matches + truncated (never a partial file).
    schema = _unique(SCHEMA_PREFIX)
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.commit()
        Base.metadata.create_all(bind=conn)
        conn.commit()
        repo_id = _insert_repo(conn, "solo/repo")
        _insert_file(conn, repo_id, "big.txt", lang=None, content="foo " * 100)
        conn.commit()

        result = grep_search(conn, "foo", max_content_bytes=10)
        assert result.files == ()
        assert result.truncated is True
        assert result.truncation_reason == "byte_cap"
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


# ------------------------------------------------------------------- 8. row cap -> truncated


@pytest.mark.integration
def test_row_cap_truncates_result(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "foo", row_limit=1)
    assert result.truncated is True
    assert result.truncation_reason == "row_cap"
    assert len(result.files) <= 1


# --------------------------------------------------------------------------- 9. no match


@pytest.mark.integration
def test_no_match_query_is_complete_and_untruncated(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "zzznotpresentzzz")
    assert result.files == ()
    assert result.truncated is False
    assert result.truncation_reason is None
