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
from sqlalchemy.exc import DataError

from app.db.client import create_db_engine
from app.db.models import Base, File, Repo
from app.query.compiler import compile_query
from app.query.parser import parse, resolve_case
from app.search.errors import QueryTooBroadError
from app.search.grep import FileCursor, GrepResult, grep_search
from indexer.hashing import content_sha

SCHEMA_PREFIX = "test_grep"


class Seeded(NamedTuple):
    conn: Connection
    acme_id: int
    beta_id: int


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _insert_repo(conn: Connection, name: str, *, default_branch: str | None = "main") -> int:
    return conn.execute(
        insert(Repo).values(name=name, default_branch=default_branch).returning(Repo.id)
    ).scalar_one()


def _insert_file(
    conn: Connection,
    repo_id: int,
    path: str,
    *,
    lang: str | None,
    content: str | None,
    branches: list[str] | None = None,
) -> int:
    # branches defaults to ["main"], matching _insert_repo's default_branch="main" -- every
    # existing (pre-0003) test keeps seeing its files via the implicit default-branch conjunct.
    return conn.execute(
        insert(File)
        .values(
            repo_id=repo_id,
            path=path,
            lang=lang,
            content=content,
            content_sha=content_sha(content),
            branches=branches if branches is not None else ["main"],
        )
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
        # Markdown file: exists solely so `file:.md` -- the query from the reported issue #31
        # incident -- has a genuine SQL-predicate match to return zero highlights for. Its
        # content deliberately contains NO "foo"/"Foo" and nothing matching /f.o/ (it has no
        # "f" at all), so adding it leaves every existing exact-path assertion above untouched.
        _insert_file(
            conn,
            beta_id,
            "docs/readme.md",
            lang="markdown",
            content="# Beta tools\n\nSome prose about the widget library.\n",
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
    # An ordinary content query proves neither query-shape condition (#31).
    assert result.no_content_atom is False
    assert result.zero_width_only_atoms is False

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


@pytest.mark.integration
@pytest.mark.xfail(
    raises=DataError,
    strict=False,
    reason="Follow-up issue #75 (filed while scoping #70's polarity-awareness work). A "
    "Postgres-invalid POSIX regex (e.g. `[`) reaches the DB raw regardless of polarity -- "
    "the compiler never validates a Regex atom's pattern, negated or not -- and Postgres's "
    "InvalidRegularExpression is a Data Exception, which SQLAlchemy surfaces as "
    "sqlalchemy.exc.DataError: a sibling class reraise_or_query_too_broad's "
    "`except OperationalError` does not catch, so it propagates uncaught as an unhandled "
    "fault instead of a recoverable payload field (see app/search/grep.py's NOT-RE2 "
    "caveat). Asserts the DESIRED future behavior (no uncaught exception) so a fix makes "
    "this XPASS (non-gating here) rather than silently rotting as a stale pin.",
)
def test_negated_broken_regex_reaching_postgres_is_an_unhandled_fault(seeded: Seeded) -> None:
    grep_search(seeded.conn, "-/[/ foo")


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
    # A TRUE negative: zero files with both shape flags False is what makes the flags mean
    # something when they DO fire (see the filter-only test below).
    assert result.no_content_atom is False
    assert result.zero_width_only_atoms is False


# ------------------------------------------------------------ 10. query-shape flags (#31)


@pytest.mark.integration
@pytest.mark.parametrize("query", ["file:.md", "file:.py"])
def test_filter_only_query_returns_no_files_but_flags_no_content_atom(
    seeded: Seeded, query: str
) -> None:
    # LITERAL reproduction of the reported bug. `file:.md` is verbatim the query from the
    # incident: an agent ran it, got zero files, and concluded the repo had no markdown -- the
    # seeded corpus DOES contain docs/readme.md, so the SQL predicate matches and the empty
    # result comes purely from having nothing to highlight. Previously indistinguishable from
    # "no such file exists"; now announced. Keep `.md` first and keep it verbatim: if this
    # test ever fails, the reader should see the reported bug, not have to reconstruct it.
    # `.py` rides along for a second extension's worth of coverage. [AC1]
    result = grep_search(seeded.conn, query)
    assert result.files == ()
    assert result.no_content_atom is True
    assert result.zero_width_only_atoms is False
    assert result.regex_incompatible is False


@pytest.mark.integration
@pytest.mark.parametrize("query", [r"/^/", r"/\b/"])
def test_zero_width_only_regex_flags_without_regex_incompatible(seeded: Seeded, query: str) -> None:
    # These compile fine (so regex_incompatible stays False) but every span is dropped as a
    # zero-width match, leaving an empty result the old envelope could not explain. [AC3]
    result = grep_search(seeded.conn, query)
    assert result.files == ()
    assert result.zero_width_only_atoms is True
    assert result.regex_incompatible is False
    assert result.no_content_atom is False


@pytest.mark.integration
def test_content_query_sets_neither_flag(seeded: Seeded) -> None:
    # Same corpus as the grouping test: real matches, neither flag. [AC2]
    result = grep_search(seeded.conn, "Handler")
    assert result.files
    assert result.no_content_atom is False
    assert result.zero_width_only_atoms is False


@pytest.mark.integration
def test_or_true_negative_does_not_set_either_flag(seeded: Seeded) -> None:
    # OR lowers to a UNION candidate set, so `lang:go` supplies candidates while the content
    # atom matches nothing -> zero files with a NON-EMPTY candidate set. A runtime
    # "matched but produced no highlights" heuristic fires here, on an ordinary true negative;
    # a provable flag must not. Pins that the rejected heuristic has not crept back in.
    result = grep_search(seeded.conn, "zzz_nonexistent OR lang:go")
    assert result.files == ()
    assert result.no_content_atom is False
    assert result.zero_width_only_atoms is False


# ------------------------------------------------------------------- 11. branch scoping (0003)


@pytest.mark.integration
def test_grep_default_query_excludes_feature_only_content(seeded: Seeded) -> None:
    # A path with two content versions: "main" (default, matched by default query) and
    # "feature" (only reachable via branch:). No branch: atom -> implicit default conjunct.
    _insert_file(
        seeded.conn,
        seeded.acme_id,
        "src/multi.go",
        lang="go",
        content="quux on main",
        branches=["main"],
    )
    _insert_file(
        seeded.conn,
        seeded.acme_id,
        "src/multi.go",
        lang="go",
        content="quux on feature",
        branches=["feature"],
    )
    seeded.conn.commit()

    default_result = grep_search(seeded.conn, "quux")
    assert _paths(default_result) == ["src/multi.go"]
    (multi,) = [f for f in default_result.files if f.path == "src/multi.go"]
    assert multi.branches == ("main",)


@pytest.mark.integration
def test_grep_branch_filter_returns_only_named_branch_content(seeded: Seeded) -> None:
    _insert_file(
        seeded.conn,
        seeded.acme_id,
        "src/multi.go",
        lang="go",
        content="quux on main",
        branches=["main"],
    )
    _insert_file(
        seeded.conn,
        seeded.acme_id,
        "src/multi.go",
        lang="go",
        content="quux on feature",
        branches=["feature"],
    )
    seeded.conn.commit()

    result = grep_search(seeded.conn, "branch:feature quux")
    (multi,) = [f for f in result.files if f.path == "src/multi.go"]
    assert multi.branches == ("feature",)
    (line,) = multi.line_matches
    assert "feature" in line.line_text


# ------------------------------------------------------------- 11. keyset cursor pagination
#
# The `foo` query over the `seeded` corpus, ordered by (repo_id, path), is deterministically
# ["src/handler.go", "src/util.go", "pkg/note.py"] (both acme files, then the beta file --
# see test 1). These pin grep_search's own `cursor` kwarg: mode gating (bare call vs
# `cursor=` supplied at all, including `None`), the row-value WHERE predicate, and the
# last-CANDIDATE-consumed resume key (issue #35 A2).


@pytest.mark.integration
def test_pagination_bare_call_is_byte_identical_to_legacy(seeded: Seeded) -> None:
    # No `cursor` kwarg at all: row-capped still means truncated=True/"row_cap", exactly the
    # pre-#35 contract (see test_row_cap_truncates_result above) -- this pins the SAME
    # assertion is unaffected by cursor's mere existence as a parameter.
    result = grep_search(seeded.conn, "foo", row_limit=2)
    assert result.truncated is True
    assert result.truncation_reason == "row_cap"


@pytest.mark.integration
def test_pagination_page_one_cursor_none_matches_bare_call_files(seeded: Seeded) -> None:
    # Page 1 (cursor=None) issues the exact query a bare call does -- same files, same order.
    bare = grep_search(seeded.conn, "foo")
    paginated = grep_search(seeded.conn, "foo", cursor=None)
    assert _paths(bare) == _paths(paginated)


@pytest.mark.integration
def test_pagination_row_cap_page_sets_truncated_false_with_resumable_cursor(seeded: Seeded) -> None:
    page1 = grep_search(seeded.conn, "foo", row_limit=2, cursor=None)
    assert _paths(page1) == ["src/handler.go", "src/util.go"]
    # Row-cap fill in pagination mode: NOT an error banner -- there is a next page.
    assert page1.truncated is False
    assert page1.truncation_reason is None
    util_sha = content_sha("// 你好 foo trailing multibyte line\nno match on this line\n")
    assert page1.next_cursor == FileCursor(seeded.acme_id, "src/util.go", util_sha)


@pytest.mark.integration
def test_pagination_second_page_resumes_after_cursor_with_no_overlap(seeded: Seeded) -> None:
    page1 = grep_search(seeded.conn, "foo", row_limit=2, cursor=None)
    page2 = grep_search(seeded.conn, "foo", row_limit=2, cursor=page1.next_cursor)
    assert _paths(page2) == ["pkg/note.py"]
    assert set(_paths(page1)) & set(_paths(page2)) == set()  # disjoint


@pytest.mark.integration
def test_pagination_exhaustion_sets_next_cursor_none(seeded: Seeded) -> None:
    page1 = grep_search(seeded.conn, "foo", row_limit=2, cursor=None)
    page2 = grep_search(seeded.conn, "foo", row_limit=2, cursor=page1.next_cursor)
    assert page2.truncated is False
    assert page2.next_cursor is None  # the candidate set is smaller than row_limit -> exhausted


@pytest.mark.integration
def test_pagination_walk_covers_full_result_set_exactly_once(seeded: Seeded) -> None:
    # One-candidate-at-a-time walk: exercises every page boundary (three files -> 4 requests,
    # the last returning zero candidates) and pins the union/order/no-duplicates property.
    all_paths: list[str] = []
    cursor: FileCursor | None = None
    for _ in range(10):  # generous bound; a stuck cursor would fail the assertion below, not hang
        page = grep_search(seeded.conn, "foo", row_limit=1, cursor=cursor)
        all_paths.extend(_paths(page))
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    else:
        pytest.fail("pagination did not exhaust within 10 pages")
    assert all_paths == ["src/handler.go", "src/util.go", "pkg/note.py"]


@pytest.mark.integration
def test_pagination_byte_cap_trips_mid_page_cursor_resumes_after_last_consumed() -> None:
    # A controlled two-file corpus (own throwaway schema, like the byte-cap tests above) so the
    # cap trips on the SECOND file, not the first: proves next_cursor resumes after the last
    # candidate actually scanned, not the one that tripped the cap (which is never re-emitted
    # as "consumed" -- it gets re-fetched on the next page instead).
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
        small_content = "foo " * 5
        _insert_file(conn, repo_id, "a.txt", lang=None, content=small_content)
        _insert_file(conn, repo_id, "b.txt", lang=None, content="foo " * 500)
        conn.commit()

        cap = len(small_content.encode("utf-8")) + 1  # fits a.txt, not a.txt + b.txt
        result = grep_search(conn, "foo", max_content_bytes=cap, cursor=None)
        assert _paths(result) == ["a.txt"]
        assert result.truncated is True
        assert result.truncation_reason == "byte_cap"
        assert result.next_cursor == FileCursor(repo_id, "a.txt", content_sha(small_content))
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_pagination_page_two_stable_under_interleaved_unrelated_repo_write(seeded: Seeded) -> None:
    page1 = grep_search(seeded.conn, "foo", row_limit=2, cursor=None)
    assert _paths(page1) == ["src/handler.go", "src/util.go"]

    # A write to a brand-new (unrelated) repo, interleaved between page fetches. Its serial id
    # is necessarily greater than every existing repo id, so it sorts AFTER acme/beta in
    # (repo_id, path) order -- landing on page 2, never page 1.
    gamma_id = _insert_repo(seeded.conn, "gamma/new")
    _insert_file(seeded.conn, gamma_id, "z.go", lang="go", content="foo interleaved\n")
    seeded.conn.commit()

    page2 = grep_search(seeded.conn, "foo", row_limit=2, cursor=page1.next_cursor)
    assert "src/handler.go" not in _paths(page2)
    assert "src/util.go" not in _paths(page2)
    assert _paths(page2) == ["pkg/note.py", "z.go"]


# ------------------------------------------------------------------- 12. negation (issue #70)
#
# The seeded corpus has no "bar" anywhere, so `-bar` alone never restricts candidacy in these
# tests -- it isolates the negation-specific behavior (no highlight pattern, shape flags,
# cap/timeout interaction) from also having to reason about which files an exclusion drops.


@pytest.mark.integration
def test_negative_content_atom_excludes_matching_file_end_to_end(seeded: Seeded) -> None:
    both = "foo and bar together\n"
    _insert_file(seeded.conn, seeded.acme_id, "src/z_both.go", lang="go", content=both)
    seeded.conn.commit()

    result = grep_search(seeded.conn, "foo -bar")
    assert "src/z_both.go" not in _paths(result)
    assert set(_paths(result)) == {"src/handler.go", "src/util.go", "pkg/note.py"}


@pytest.mark.integration
def test_zero_width_only_atoms_true_from_positive_leaf_beside_negation(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "/^/ -bar")
    assert result.zero_width_only_atoms is True
    assert result.no_content_atom is False
    assert result.regex_incompatible is False
    assert result.files == ()


@pytest.mark.integration
def test_negative_only_query_returns_no_files_with_no_content_atom(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "-bar", cursor=None)
    assert result.files == ()
    assert result.no_content_atom is True
    assert result.truncated is False
    assert result.next_cursor is None


@pytest.mark.integration
def test_row_cap_with_exclusion_still_truncates(seeded: Seeded) -> None:
    # 3 real files match "foo -bar" (handler.go, util.go, note.py); row_limit=1 caps it.
    result = grep_search(seeded.conn, "foo -bar", row_limit=1)
    assert result.truncated is True
    assert result.truncation_reason == "row_cap"


@pytest.mark.integration
def test_byte_cap_with_exclusion_still_truncates(seeded: Seeded) -> None:
    result = grep_search(seeded.conn, "foo -bar", max_content_bytes=10)
    assert result.truncated is True
    assert result.truncation_reason == "byte_cap"


@pytest.mark.integration
def test_multibyte_byte_ranges_accurate_with_a_noop_exclusion(seeded: Seeded) -> None:
    # Same corpus/assertions as test_grep_multibyte_byte_ranges_are_utf8_accurate, with a
    # harmless `-bar` tacked on: exclusion plumbing must not perturb UTF-8 byte offsets.
    result = grep_search(seeded.conn, "foo -bar")
    (util,) = [f for f in result.files if f.path == "src/util.go"]
    (m,) = util.line_matches
    assert m.line_number == 1
    (span,) = m.byte_ranges
    start, end = span
    assert m.line_text.encode("utf-8")[start:end] == b"foo"


@pytest.mark.integration
def test_tiny_statement_timeout_with_exclusion_raises_query_too_broad(seeded: Seeded) -> None:
    # Mirrors test_tiny_statement_timeout_raises_query_too_broad: the timeout guard must still
    # fire when the query also carries an exclusion (the SQL predicate is `... AND NOT (...)`,
    # not a different code path).
    blob = "a" * (2 * 1024 * 1024)
    for i in range(8):
        _insert_file(seeded.conn, seeded.acme_id, f"src/blob{i}.txt", lang=None, content=blob)
    seeded.conn.commit()
    with pytest.raises(QueryTooBroadError):
        grep_search(seeded.conn, "/zq/ -bar", statement_timeout_ms=1)


@pytest.mark.integration
def test_negated_content_predicate_does_not_resurrect_null_content_row(seeded: Seeded) -> None:
    # Three-valued SQL (README's documented contract): a row with NULL `content` matches
    # NEITHER `content:foo` NOR `-content:foo`. A NAIVE two-valued reading of "-foo" ("doesn't
    # contain foo") would wrongly treat a NULL-content row as a match; prove the compiled
    # predicate excludes it instead -- both at grep's own layer and directly against the
    # candidate SQL (grep's `content or ""` coercion would hide the row anyway once `patterns`
    # is empty, so the candidate-SQL check is the one that actually proves this).
    _insert_file(seeded.conn, seeded.acme_id, "src/nullish.go", lang="go", content=None)
    seeded.conn.commit()

    result = grep_search(seeded.conn, "lang:go -foo")
    assert result.files == ()
    assert result.no_content_atom is True

    query = "lang:go -foo"
    node = parse(query)
    stmt = compile_query(node, limit=200, case_sensitive=resolve_case(query))
    candidate_paths = {row.path for row in seeded.conn.execute(stmt).all()}
    assert "src/nullish.go" not in candidate_paths
    # The two real go files both contain "foo" -> excluded by -foo, not by the null check --
    # so candidate_paths is empty for an entirely different (and also correct) reason.
    assert candidate_paths == set()
