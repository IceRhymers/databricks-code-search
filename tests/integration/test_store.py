"""Integration tests for indexer.store.index_repo against a real local Postgres.

Reuses the throwaway-schema fixture style from test_db_client.py: a clean schema
per run, the durable-core DDL via ``Base.metadata.create_all``, and a per-connection
``search_path`` that propagates into ``index_repo``'s DML. The fixture connection is
passed directly to ``index_repo`` (the injected-connection seam).

Multi-branch (0003+): ``index_repo`` now writes ONE ``(repo, branch)`` per call.
Every existing single-branch scenario is exercised by calling it with
``branch="main", is_default=True`` -- the pre-multi-branch behavior, now
expressed through the new signature. The new scenarios (shared/divergent
content across branches, per-branch CAS, the empty-seen-set guard) cover
multi-branch indexing end to end.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator

import pytest
from sqlalchemy import Connection, text

from app.db.client import create_db_engine
from app.db.grants import build_job_grants
from app.db.models import INDEX_SEMANTICS_VERSION, Base
from indexer.languages import ExtractedSymbol, IndexCounts, ParsedFile
from indexer.store import StaleIndexError, _stamp_repo_branch, index_repo

SCHEMA = "test_store"


@pytest.fixture
def conn() -> Iterator[Connection]:
    engine = create_db_engine()
    connection = engine.connect()
    try:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
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


def _pf(path: str, content: str) -> ParsedFile:
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


def _items(
    *specs: tuple[str, str, list[ExtractedSymbol]],
) -> list[tuple[ParsedFile, list[ExtractedSymbol]]]:
    return [(_pf(path, content), syms) for path, content, syms in specs]


MAIN = ("main.py", "def f():\n    return 1\n", [ExtractedSymbol("f", "function", 1, 2)])
UTIL = ("util.py", "def g():\n    return 2\n", [ExtractedSymbol("g", "function", 1, 2)])


def _count(conn: Connection, table: str, where: str = "") -> int:
    sql = f"SELECT count(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(text(sql)).scalar_one())


def _index_default(
    conn: Connection,
    *,
    name: str,
    head_sha: str,
    items: Iterable[tuple[ParsedFile, list[ExtractedSymbol]]],
) -> IndexCounts:
    """Shorthand for the pre-multi-branch call shape: one default branch, "main"."""
    return index_repo(
        conn, name=name, branch="main", is_default=True, head_sha=head_sha, items=items
    )


@pytest.mark.integration
def test_first_run_populates_and_stamps_commit(conn: Connection) -> None:
    counts = _index_default(
        conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN, UTIL)
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0)

    assert _count(conn, "repos") == 1
    assert _count(conn, "files") == 2
    assert _count(conn, "symbols") == 2
    assert _count(conn, "files", "commit <> 'sha_first'") == 0

    repo = conn.execute(
        text("SELECT default_branch, last_indexed_commit FROM repos WHERE name = 'acme/widgets'")
    ).one()
    assert repo == ("main", "sha_first")

    branches = conn.execute(text("SELECT branches FROM files ORDER BY path")).scalars().all()
    assert all(b == ["main"] for b in branches)

    rb = conn.execute(
        text(
            "SELECT rb.branch, rb.last_indexed_commit FROM repo_branches rb "
            "JOIN repos r ON r.id = rb.repo_id WHERE r.name = 'acme/widgets'"
        )
    ).one()
    assert rb == ("main", "sha_first")


@pytest.mark.integration
def test_rerun_is_idempotent(conn: Connection) -> None:
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN, UTIL))
    counts = _index_default(
        conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN, UTIL)
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0)
    assert _count(conn, "repos") == 1
    assert _count(conn, "files") == 2
    assert _count(conn, "symbols") == 2


@pytest.mark.integration
def test_mark_and_sweep_removes_deleted_file(conn: Connection) -> None:
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN, UTIL))
    repo_id = conn.execute(text("SELECT id FROM repos WHERE name = 'acme/widgets'")).scalar_one()
    removed_file_id = conn.execute(text("SELECT id FROM files WHERE path = 'util.py'")).scalar_one()
    assert _count(conn, "symbols", f"file_id = {removed_file_id}") == 1

    # index_repo has no reference_edges writer yet (#84); seed one directly to
    # prove the sweep's FK cascade reaches this table too.
    conn.execute(
        text(
            "INSERT INTO reference_edges (repo_id, file_id, edge_kind, target_name, line) "
            "VALUES (:r, :f, 'call', 'target_fn', 1)"
        ),
        {"r": repo_id, "f": removed_file_id},
    )
    conn.commit()
    assert _count(conn, "reference_edges", f"file_id = {removed_file_id}") == 1

    # Reads above autobegan a txn; clear it so index_repo gets a clean connection
    # (production hands a fresh engine.connect() per repo).
    conn.rollback()

    # Re-run without util.py and with a new head SHA -> util.py is swept.
    counts = _index_default(conn, name="acme/widgets", head_sha="sha_second", items=_items(MAIN))
    assert counts == IndexCounts(files=1, symbols=1, swept=1)

    assert _count(conn, "files", "path = 'util.py'") == 0
    assert _count(conn, "symbols", f"file_id = {removed_file_id}") == 0  # cascade
    assert _count(conn, "reference_edges", f"file_id = {removed_file_id}") == 0  # cascade
    assert _count(conn, "files") == 1
    assert _count(conn, "files", "commit = 'sha_second'") == 1


@pytest.mark.integration
def test_sweep_is_repo_scoped(conn: Connection) -> None:
    # Two repos indexed at their own SHAs; re-indexing repo A with a new SHA must
    # sweep only A's stale rows and never touch repo B.
    _index_default(conn, name="acme/a", head_sha="a_first", items=_items(MAIN, UTIL))
    conn.rollback()
    _index_default(conn, name="acme/b", head_sha="b_first", items=_items(UTIL))
    conn.rollback()

    b_files_before = _count(conn, "files", "repo_id = (SELECT id FROM repos WHERE name = 'acme/b')")
    conn.rollback()

    # Re-index A without util.py at a new SHA -> A's util.py swept, B untouched.
    counts = _index_default(conn, name="acme/a", head_sha="a_second", items=_items(MAIN))
    assert counts == IndexCounts(files=1, symbols=1, swept=1)

    assert _count(conn, "files", "repo_id = (SELECT id FROM repos WHERE name = 'acme/b')") == (
        b_files_before
    )
    assert _count(conn, "files", "commit = 'b_first'") == 1  # B's row unchanged


@pytest.mark.integration
def test_stamp_writes_semantics_version(conn: Connection) -> None:
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN, UTIL))
    conn.rollback()
    # Second run at a new SHA succeeds against its own in-transaction baseline.
    _index_default(conn, name="acme/widgets", head_sha="sha_second", items=_items(MAIN))

    stamp = conn.execute(
        text(
            "SELECT rb.last_indexed_commit, rb.index_semantics_version FROM repo_branches rb "
            "JOIN repos r ON r.id = rb.repo_id WHERE r.name = 'acme/widgets'"
        )
    ).one()
    assert stamp == ("sha_second", INDEX_SEMANTICS_VERSION)
    # The deprecated legacy repos stamp is written in lockstep by the default run.
    legacy = conn.execute(
        text(
            "SELECT last_indexed_commit, index_semantics_version FROM repos "
            "WHERE name = 'acme/widgets'"
        )
    ).one()
    assert legacy == ("sha_second", INDEX_SEMANTICS_VERSION)


@pytest.mark.integration
def test_legacy_null_semantics_version_is_rewritten(conn: Connection) -> None:
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN))
    conn.execute(text("UPDATE repo_branches SET index_semantics_version = NULL"))
    conn.commit()

    _index_default(conn, name="acme/widgets", head_sha="sha_second", items=_items(MAIN))
    stamp = conn.execute(
        text(
            "SELECT rb.last_indexed_commit, rb.index_semantics_version FROM repo_branches rb "
            "JOIN repos r ON r.id = rb.repo_id WHERE r.name = 'acme/widgets'"
        )
    ).one()
    assert stamp == ("sha_second", INDEX_SEMANTICS_VERSION)


@pytest.mark.integration
def test_cas_predicate_rejects_wrong_baseline(conn: Connection) -> None:
    # Direct test of the compare-and-set UPDATE predicate: index normally, then
    # run the stamp with a deliberately wrong baseline. Zero rows match -> raise.
    _index_default(conn, name="acme/widgets", head_sha="sha_a", items=_items(MAIN, UTIL))
    files_before = _count(conn, "files")
    symbols_before = _count(conn, "symbols")
    repo_id = int(
        conn.execute(text("SELECT id FROM repos WHERE name = 'acme/widgets'")).scalar_one()
    )
    conn.rollback()

    # The raise propagates out of conn.begin(), which rolls the transaction back
    # exactly as it would inside index_repo.
    with pytest.raises(StaleIndexError, match="wrong_sha"), conn.begin():
        conn.execute(text("DELETE FROM files WHERE path = 'util.py'"))
        _stamp_repo_branch(
            conn,
            name="acme/widgets",
            branch="main",
            repo_id=repo_id,
            head_sha="sha_b",
            baseline_commit="wrong_sha",
            baseline_version=INDEX_SEMANTICS_VERSION,
        )

    # The aborted transaction left the index exactly as the sha_a run wrote it.
    assert _count(conn, "files") == files_before
    assert _count(conn, "symbols") == symbols_before
    assert _count(conn, "files", "commit <> 'sha_a'") == 0
    assert (
        conn.execute(
            text(
                "SELECT rb.last_indexed_commit FROM repo_branches rb "
                "JOIN repos r ON r.id = rb.repo_id WHERE r.name = 'acme/widgets'"
            )
        ).scalar_one()
        == "sha_a"
    )


@pytest.mark.integration
def test_midrun_failure_rolls_back_entirely(conn: Connection) -> None:
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN, UTIL))
    files_before = _count(conn, "files")
    symbols_before = _count(conn, "symbols")
    conn.rollback()  # clear the read txn before the next index_repo (see note above)

    def poison() -> Iterator[tuple[ParsedFile, list[ExtractedSymbol]]]:
        # First item is a NEW file that would be inserted; then blow up mid-stream
        # so the exception propagates out of index_repo's conn.begin().
        yield _pf("new.py", "def h():\n    return 3\n"), [ExtractedSymbol("h", "function", 1, 2)]
        raise RuntimeError("poison item")

    with pytest.raises(RuntimeError, match="poison item"):
        _index_default(conn, name="acme/widgets", head_sha="sha_third", items=poison())

    # Nothing committed: the new file is absent and prior rows are untouched.
    assert _count(conn, "files", "path = 'new.py'") == 0
    assert _count(conn, "files") == files_before
    assert _count(conn, "symbols") == symbols_before
    assert _count(conn, "files", "commit <> 'sha_first'") == 0


# --- Multi-branch: shared content, divergent content, array-remove ----------


@pytest.mark.integration
def test_two_branches_sharing_a_file_produce_one_row(conn: Connection) -> None:
    """Identical content on two branches -> one files row, branches=['a','b']."""
    index_repo(
        conn, name="acme/widgets", branch="a", is_default=True, head_sha="sha_a", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b",
        items=_items(MAIN),
    )

    assert _count(conn, "files") == 1
    branches = conn.execute(text("SELECT branches FROM files")).scalar_one()
    assert sorted(branches) == ["a", "b"]


@pytest.mark.integration
def test_divergent_content_produces_two_disjoint_rows(conn: Connection) -> None:
    """Same path, different content on each branch -> two rows, disjoint branches."""
    a_content = ("main.py", "def f():\n    return 1\n", [ExtractedSymbol("f", "function", 1, 2)])
    b_content = ("main.py", "def f():\n    return 2\n", [ExtractedSymbol("f", "function", 1, 2)])

    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a",
        items=_items(a_content),
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b",
        items=_items(b_content),
    )

    assert _count(conn, "files") == 2
    rows = conn.execute(text("SELECT content_sha, branches FROM files ORDER BY content_sha")).all()
    assert rows[0].content_sha != rows[1].content_sha
    all_branches = {b for row in rows for b in row.branches}
    assert all_branches == {"a", "b"}
    # Each row's branches array is disjoint from the other's.
    assert set(rows[0].branches).isdisjoint(rows[1].branches)


@pytest.mark.integration
def test_file_removed_from_one_branch_but_present_in_other_survives(conn: Connection) -> None:
    """A file present on both, then removed from A: A's array_remove leaves B's row."""
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a1",
        items=_items(MAIN, UTIL),
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b1",
        items=_items(UTIL),
    )
    conn.rollback()

    # Re-index A without util.py: A's membership in util.py's row is removed,
    # but the row survives because B still needs it.
    counts = index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a2",
        items=_items(MAIN),
    )
    assert counts.swept == 1  # array-remove counts as swept, not a delete

    assert _count(conn, "files", "path = 'util.py'") == 1
    branches = conn.execute(text("SELECT branches FROM files WHERE path = 'util.py'")).scalar_one()
    assert branches == ["b"]


@pytest.mark.integration
def test_file_removed_from_only_branch_deletes_the_row(conn: Connection) -> None:
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a1",
        items=_items(MAIN, UTIL),
    )
    conn.rollback()

    counts = index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a2",
        items=_items(MAIN),
    )
    assert counts.swept == 1
    assert _count(conn, "files", "path = 'util.py'") == 0


@pytest.mark.integration
def test_per_branch_cas_resume_is_independent_per_branch(conn: Connection) -> None:
    """Each branch's CAS baseline is its OWN repo_branches row -- no cross-branch conflict."""
    index_repo(
        conn, name="acme/widgets", branch="a", is_default=True, head_sha="sha_a", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b",
        items=_items(UTIL),
    )
    conn.rollback()

    # Re-indexing 'a' again must succeed against its own baseline, unaffected by
    # 'b' having indexed in between.
    counts = index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a2",
        items=_items(MAIN),
    )
    assert counts == IndexCounts(files=1, symbols=1, swept=0)

    stamps = dict(conn.execute(text("SELECT branch, last_indexed_commit FROM repo_branches")).all())
    assert stamps == {"a": "sha_a2", "b": "sha_b"}


@pytest.mark.integration
def test_only_default_branch_run_writes_repos_default_branch(conn: Connection) -> None:
    index_repo(
        conn, name="acme/widgets", branch="a", is_default=True, head_sha="sha_a", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b",
        items=_items(UTIL),
    )

    default_branch = conn.execute(
        text("SELECT default_branch FROM repos WHERE name = 'acme/widgets'")
    ).scalar_one()
    assert default_branch == "a"


@pytest.mark.integration
def test_empty_seen_set_skips_sweep_and_preserves_membership(conn: Connection) -> None:
    """A branch that parses zero indexable files must not wipe its own prior membership."""
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a1",
        items=_items(MAIN),
    )
    conn.rollback()

    # Re-index with an empty item set (e.g. a repo with no indexable files this run).
    counts = index_repo(
        conn, name="acme/widgets", branch="a", is_default=True, head_sha="sha_a2", items=[]
    )
    assert counts == IndexCounts(files=0, symbols=0, swept=0)

    # main.py's membership in 'a' is untouched -- the sweep was skipped, not run.
    assert _count(conn, "files", "path = 'main.py'") == 1
    branches = conn.execute(text("SELECT branches FROM files WHERE path = 'main.py'")).scalar_one()
    assert branches == ["a"]


@pytest.mark.integration
def test_empty_seen_set_does_not_touch_another_branchs_membership(conn: Connection) -> None:
    """The empty-seen-set guard is scoped to the repo, proving it doesn't clobber other branches."""
    index_repo(
        conn, name="acme/widgets", branch="a", is_default=True, head_sha="sha_a", items=_items(MAIN)
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b1",
        items=_items(UTIL),
    )
    conn.rollback()

    index_repo(conn, name="acme/widgets", branch="b", is_default=False, head_sha="sha_b2", items=[])

    assert _count(conn, "files", "path = 'main.py'") == 1
    assert _count(conn, "files", "path = 'util.py'") == 1
    util_branches = conn.execute(
        text("SELECT branches FROM files WHERE path = 'util.py'")
    ).scalar_one()
    assert util_branches == ["b"]


# --- Sweep under the actual least-privilege job role (not the superuser fixture) --
# The membership sweep is pure DML with no TEMP TABLE specifically because the job
# role has no guaranteed database-level TEMP privilege on Lakebase (see
# app/db/grants.py). Running under the superuser fixture connection would let a
# TEMP-table regression pass here and fail only in prod -- so this test actually
# switches role via SET ROLE, the same technique test_migrations.py uses.


@pytest.mark.integration
def test_sweep_runs_under_the_actual_job_role(conn: Connection) -> None:
    job_role = f"test_job_wr_{uuid.uuid4().hex[:12]}"
    conn.execute(text(f"CREATE ROLE {job_role} NOLOGIN"))
    for stmt in build_job_grants(SCHEMA, job_role):
        conn.execute(text(stmt))
    # index_repo's file upserts also need USAGE on the id sequences, which
    # Base.metadata.create_all did not grant -- build_job_grants covers exactly
    # what a real deploy would apply.
    conn.commit()
    try:
        conn.execute(text(f"SET ROLE {job_role}"))
        conn.execute(text(f"SET search_path TO {SCHEMA}, public"))
        conn.commit()

        index_repo(
            conn,
            name="acme/widgets",
            branch="a",
            is_default=True,
            head_sha="sha_a1",
            items=_items(MAIN, UTIL),
        )
        conn.commit()

        # The sweep: util.py drops out of branch 'a' -> array_remove, then
        # DELETE FROM files WHERE cardinality(branches) = 0. Both pure DML,
        # neither needing TEMP TABLE privilege.
        counts = index_repo(
            conn,
            name="acme/widgets",
            branch="a",
            is_default=True,
            head_sha="sha_a2",
            items=_items(MAIN),
        )
        conn.commit()

        assert counts.swept == 1
        assert _count(conn, "files", "path = 'util.py'") == 0
        assert _count(conn, "files", "path = 'main.py'") == 1
    finally:
        conn.rollback()
        conn.execute(text("RESET ROLE"))
        conn.execute(text(f"DROP OWNED BY {job_role} CASCADE"))
        conn.execute(text(f"DROP ROLE IF EXISTS {job_role}"))
        conn.commit()
