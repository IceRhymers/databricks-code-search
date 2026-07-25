"""Integration tests for the file-level delta path (issue #104) against real Postgres.

Row-identity proof -- that skipping a file really does leave its stored serials
untouched -- is this module's job; ``tests/unit/test_store_delta.py`` pins the
exact *statement inventory* each classification produces against a fake
connection. Clones ``tests/integration/test_store.py``'s throwaway-schema fixture
idiom (own copy, per ``tests/integration/AGENTS.md``'s no-conftest convention):
a clean schema per run, the durable-core DDL via ``Base.metadata.create_all``,
and a per-connection ``search_path`` that propagates into ``index_repo``'s DML.

**Deliberately builds no ``chunks`` table and creates no ``lakebase_*``
extension.** That is the entire reason this module runs locally at all --
``test_store_chunk_writer.py`` errors at *module fixture* setup on
``CREATE EXTENSION IF NOT EXISTS lakebase_vector CASCADE``, which no local
Postgres image provides. Chunk-touching delta cases live there instead
(Lakebase-deferred; see that module's docstring).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.db.client import create_db_engine
from app.db.models import INDEX_SEMANTICS_VERSION, Base
from indexer.languages import ExtractedSymbol, FileExtraction, IndexCounts, ParsedFile
from indexer.store import index_repo

SCHEMA = "test_store_delta"


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


def _pf(path: str, content: str) -> ParsedFile:
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


def _items(
    *specs: tuple[str, str, list[ExtractedSymbol]],
) -> list[tuple[ParsedFile, FileExtraction]]:
    return [
        (_pf(path, content), FileExtraction(symbols=syms, edges=[]))
        for path, content, syms in specs
    ]


def _fn(name: str, n: int) -> tuple[str, str, list[ExtractedSymbol]]:
    """One deterministic (path, content, symbols) triple, distinguishable by ``n``."""
    content = f"def f{n}():\n    return {n}\n"
    return (f"{name}{n}.py", content, [ExtractedSymbol(f"f{n}", "function", 1, 2)])


MAIN = ("main.py", "def f():\n    return 1\n", [ExtractedSymbol("f", "function", 1, 2)])
UTIL = ("util.py", "def g():\n    return 2\n", [ExtractedSymbol("g", "function", 1, 2)])


def _index_default(
    conn: Connection, *, name: str, head_sha: str, items: list[tuple[ParsedFile, FileExtraction]]
) -> IndexCounts:
    return index_repo(
        conn, name=name, branch="main", is_default=True, head_sha=head_sha, items=items
    )


def _count(conn: Connection, table: str, where: str = "") -> int:
    sql = f"SELECT count(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    return int(conn.execute(text(sql)).scalar_one())


def _symbol_ids(conn: Connection, path: str) -> list[int]:
    return sorted(
        conn.execute(
            text("SELECT s.id FROM symbols s JOIN files f ON f.id = s.file_id WHERE f.path = :p"),
            {"p": path},
        )
        .scalars()
        .all()
    )


def _delta_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "indexer.store" and "delta write set" in r.getMessage()
    ]


# --- Test 15: row-identity proof, the "unchanged" class writes NOTHING ------


@pytest.mark.integration
def test_unchanged_rerun_preserves_every_row_identity(
    conn: Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """Re-index identical content at a NEW head_sha: files.id and symbols.id are
    IDENTICAL before/after -- a delete-reinsert would renumber the serials, so
    identical ids are the precise proof that zero rows were written.
    ``counts.symbols == 0`` while ``counts.files == N``, and the
    ``delta write set 0/N`` line is emitted.
    """
    items = _items(MAIN, UTIL)
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=items)
    files_before = dict(conn.execute(text("SELECT path, id FROM files ORDER BY path")).all())
    symbols_before = sorted(conn.execute(text("SELECT id FROM symbols")).scalars().all())
    conn.rollback()

    with caplog.at_level(logging.INFO, logger="indexer.store"):
        counts = _index_default(
            conn, name="acme/widgets", head_sha="sha_second", items=_items(MAIN, UTIL)
        )
    assert counts == IndexCounts(files=2, symbols=0, swept=0, edges=0)

    files_after = dict(conn.execute(text("SELECT path, id FROM files ORDER BY path")).all())
    symbols_after = sorted(conn.execute(text("SELECT id FROM symbols")).scalars().all())
    assert files_after == files_before
    assert symbols_after == symbols_before

    lines = _delta_lines(caplog)
    assert len(lines) == 1
    assert "delta write set 0/2 files (unchanged=2 membership=0, semantics gate open)" in lines[0]


# --- Test 16: small delta (1 changed, 1 added, 1 deleted) -- issue AC2 -------


@pytest.mark.integration
def test_small_delta_writes_only_the_changed_added_and_sweeps_the_deleted(conn: Connection) -> None:
    """1 changed, 1 unchanged, 1 added, 1 deleted -- the changed file's symbol ids
    change, the unchanged file's do not, the added file is present, and the
    deleted file is swept. Issue #104's acceptance criterion 2.
    """
    unchanged_symbol = ExtractedSymbol("f", "function", 1, 2)
    changed_v1 = (
        "changed.py",
        "def c():\n    return 1\n",
        [ExtractedSymbol("c", "function", 1, 2)],
    )
    to_delete = ("gone.py", "def d():\n    return 1\n", [ExtractedSymbol("d", "function", 1, 2)])
    _index_default(
        conn,
        name="acme/widgets",
        head_sha="sha_first",
        items=_items(
            ("unchanged.py", "def f():\n    return 1\n", [unchanged_symbol]), changed_v1, to_delete
        ),
    )
    unchanged_ids_before = _symbol_ids(conn, "unchanged.py")
    changed_ids_before = _symbol_ids(conn, "changed.py")
    assert unchanged_ids_before and changed_ids_before
    conn.rollback()

    changed_v2 = (
        "changed.py",
        "def c():\n    return 2\n",
        [ExtractedSymbol("c", "function", 1, 2)],
    )
    added = ("added.py", "def a():\n    return 1\n", [ExtractedSymbol("a", "function", 1, 2)])
    counts = _index_default(
        conn,
        name="acme/widgets",
        head_sha="sha_second",
        items=_items(
            ("unchanged.py", "def f():\n    return 1\n", [unchanged_symbol]), changed_v2, added
        ),
    )
    # swept=2: gone.py (removed outright) AND changed.py's OLD content_sha row
    # (the changed-file upsert mints a NEW row under the new content_sha, since
    # uq_files_repo_path_sha is keyed on content -- the stale old-sha row is
    # exactly what the sweep exists to reap, unrelated to delta).
    assert counts == IndexCounts(files=3, symbols=2, swept=2, edges=0)

    assert _symbol_ids(conn, "unchanged.py") == unchanged_ids_before
    assert _symbol_ids(conn, "changed.py") != changed_ids_before
    assert _count(conn, "files", "path = 'added.py'") == 1
    assert _count(conn, "files", "path = 'gone.py'") == 0


# --- Test 17: multi-branch dedup takes the membership-only path -- AC3 ------


@pytest.mark.integration
def test_membership_only_dedup_across_branches_preserves_symbol_ids(
    conn: Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """Branch 'b' acquires content already stored under branch 'a': ``branches``
    becomes ``['a', 'b']`` (sorted, matching the array_agg(DISTINCT ...) idiom),
    symbol ids are UNCHANGED (no rewrite), and the delta write set line reports
    ``membership=1``. Issue #104's acceptance criterion 3.
    """
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a1",
        items=_items(MAIN),
    )
    conn.rollback()
    # branch 'b' must ALREADY be at the current semantics version (its own
    # baseline) for the delta gate to be open when it next acquires MAIN --
    # a brand-new branch's first run is always full-path.
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b1",
        items=_items(UTIL),
    )
    main_ids_before = _symbol_ids(conn, "main.py")
    assert main_ids_before
    conn.rollback()

    with caplog.at_level(logging.INFO, logger="indexer.store"):
        counts = index_repo(
            conn,
            name="acme/widgets",
            branch="b",
            is_default=False,
            head_sha="sha_b2",
            items=_items(MAIN, UTIL),
        )
    assert counts == IndexCounts(files=2, symbols=0, swept=0, edges=0)

    assert _symbol_ids(conn, "main.py") == main_ids_before
    branches = conn.execute(text("SELECT branches FROM files WHERE path = 'main.py'")).scalar_one()
    assert sorted(branches) == ["a", "b"]

    lines = _delta_lines(caplog)
    assert len(lines) == 1
    assert "membership=1" in lines[0]


@pytest.mark.integration
def test_membership_only_row_gets_no_symbol_or_edge_statement(conn: Connection) -> None:
    """Companion to the row-identity proof above, phrased as a statement-absence
    check rather than an id-equality check: the acquiring branch's run inserts
    NO symbols row for the acquired file at all (there was never a duplicate to
    delete-and-reinsert)."""
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a1",
        items=_items(MAIN),
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
    symbols_before = _count(conn, "symbols")
    conn.rollback()

    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b2",
        items=_items(MAIN, UTIL),
    )
    assert _count(conn, "symbols") == symbols_before


# --- Test 18: a semantics-version mismatch forces the full path -- AC4 ------


@pytest.mark.integration
def test_stale_semantics_version_forces_the_full_path_for_every_file(conn: Connection) -> None:
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN, UTIL))
    main_ids_before = _symbol_ids(conn, "main.py")
    util_ids_before = _symbol_ids(conn, "util.py")
    conn.execute(
        text("UPDATE repo_branches SET index_semantics_version = :v"),
        {"v": INDEX_SEMANTICS_VERSION - 1},
    )
    conn.commit()

    counts = _index_default(
        conn, name="acme/widgets", head_sha="sha_second", items=_items(MAIN, UTIL)
    )
    assert counts == IndexCounts(files=2, symbols=2, swept=0, edges=0)
    assert _symbol_ids(conn, "main.py") != main_ids_before
    assert _symbol_ids(conn, "util.py") != util_ids_before


# --- Test 19: the provenance gate -- a stale SIBLING branch closes it -------


@pytest.mark.integration
def test_provenance_gate_forces_the_full_path_when_a_sibling_branch_is_stale(
    conn: Connection,
) -> None:
    """The counter-example the provenance gate (statement 4) exists to close:
    'stale_sibling' wrote main.py at the current version, then regressed to an
    older one (simulating a failed re-index). 'acquirer' is itself at the
    current version and tries to acquire main.py membership-only -- but every
    ``repo_branches`` row for this repo must be current for that path to be
    taken, and stale_sibling's is not, so 'acquirer' gets the FULL path
    instead (proven by main.py's symbol ids changing under 'acquirer').
    """
    index_repo(
        conn,
        name="acme/widgets",
        branch="stale_sibling",
        is_default=True,
        head_sha="sha_s1",
        items=_items(MAIN),
    )
    main_ids_before = _symbol_ids(conn, "main.py")
    conn.execute(
        text(
            "UPDATE repo_branches SET index_semantics_version = :v WHERE branch = 'stale_sibling'"
        ),
        {"v": INDEX_SEMANTICS_VERSION - 1},
    )
    conn.commit()

    index_repo(
        conn,
        name="acme/widgets",
        branch="acquirer",
        is_default=False,
        head_sha="sha_a1",
        items=_items(UTIL),
    )
    conn.rollback()

    index_repo(
        conn,
        name="acme/widgets",
        branch="acquirer",
        is_default=False,
        head_sha="sha_a2",
        items=_items(MAIN, UTIL),
    )

    assert _symbol_ids(conn, "main.py") != main_ids_before
    branches = conn.execute(text("SELECT branches FROM files WHERE path = 'main.py'")).scalar_one()
    assert sorted(branches) == ["acquirer", "stale_sibling"]


# --- Tests 20a/20b: the membership invariant, both directions --------------


@pytest.mark.integration
def test_no_files_row_ever_has_an_empty_branches_array(conn: Connection) -> None:
    """Forward direction: both sweep sites (the per-branch sweep and the
    array-remove path) delete a row once its branches array is emptied, never
    leaving a zombie row behind. Exercised across additions, a shared-then-
    removed-from-one-branch file, and a fully-removed file.
    """
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
    # 'a' drops both files -- util.py loses only 'a' (shared with 'b'), main.py
    # loses its only branch and must be deleted outright.
    index_repo(conn, name="acme/widgets", branch="a", is_default=True, head_sha="sha_a2", items=[])
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a3",
        items=_items(UTIL),
    )
    conn.rollback()
    index_repo(conn, name="acme/widgets", branch="a", is_default=True, head_sha="sha_a4", items=[])

    assert _count(conn, "files", "cardinality(branches) = 0") == 0


@pytest.mark.integration
def test_every_branch_on_a_files_row_has_a_repo_branches_row(conn: Connection) -> None:
    """Converse direction: index_repo writes statement 2 (the repo_branches
    upsert) before any file row for that branch, so no branches array element
    can ever dangle without a matching repo_branches row. The provenance gate
    (statement 4) depends on this holding."""
    index_repo(
        conn,
        name="acme/widgets",
        branch="a",
        is_default=True,
        head_sha="sha_a1",
        items=_items(MAIN),
    )
    conn.rollback()
    index_repo(
        conn,
        name="acme/widgets",
        branch="b",
        is_default=False,
        head_sha="sha_b1",
        items=_items(MAIN, UTIL),
    )

    dangling = conn.execute(
        text(
            "SELECT count(*) FROM files f, unnest(f.branches) AS b "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM repo_branches rb WHERE rb.repo_id = f.repo_id AND rb.branch = b"
            ")"
        )
    ).scalar_one()
    assert dangling == 0


# --- Test 21: empty-seen-set guard and per-branch CAS still hold with delta -


@pytest.mark.integration
def test_empty_seen_set_guard_holds_with_the_delta_gate_open(conn: Connection) -> None:
    """The empty-seen-set guard (skip the sweep, WARN, return 0) is delta-blind by
    construction -- it fires on ``seen_paths`` being empty, before any
    classification runs. Pinned again here because it is exactly the run shape
    the delta gate makes common (a branch whose HEAD moved but touched nothing
    indexable)."""
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN))
    conn.rollback()
    # Second run: delta gate is open (first run stamped INDEX_SEMANTICS_VERSION),
    # but this run parses zero files.
    counts = _index_default(conn, name="acme/widgets", head_sha="sha_second", items=[])
    assert counts == IndexCounts(files=0, symbols=0, swept=0, edges=0)
    assert _count(conn, "files", "path = 'main.py'") == 1
    branches = conn.execute(text("SELECT branches FROM files WHERE path = 'main.py'")).scalar_one()
    assert branches == ["main"]


@pytest.mark.integration
def test_cas_still_rejects_a_stale_baseline_with_delta_on(conn: Connection) -> None:
    """The CAS predicate is statement 5, downstream of every delta statement --
    proven still load-bearing by forcing a conflict on a branch whose delta gate
    is open (second run onward)."""
    from indexer.store import StaleIndexError, _stamp_repo_branch

    _index_default(conn, name="acme/widgets", head_sha="sha_a", items=_items(MAIN, UTIL))
    conn.rollback()
    _index_default(conn, name="acme/widgets", head_sha="sha_b", items=_items(MAIN, UTIL))
    files_before = _count(conn, "files")
    repo_id = int(
        conn.execute(text("SELECT id FROM repos WHERE name = 'acme/widgets'")).scalar_one()
    )
    conn.rollback()

    with pytest.raises(StaleIndexError, match="wrong_sha"), conn.begin():
        conn.execute(text("DELETE FROM files WHERE path = 'util.py'"))
        _stamp_repo_branch(
            conn,
            name="acme/widgets",
            branch="main",
            repo_id=repo_id,
            head_sha="sha_c",
            baseline_commit="wrong_sha",
            baseline_version=INDEX_SEMANTICS_VERSION,
        )
    assert _count(conn, "files") == files_before


# --- Test 22 (BLOCKER): a zero-parse run must NOT advance the semantics stamp


@pytest.mark.integration
def test_zero_parse_run_does_not_advance_the_semantics_stamp(conn: Connection) -> None:
    """The base-case fix (§2.3): index a branch non-empty, force its stored
    version DOWN to simulate a pre-transition stamp, then re-index with
    ``items=[]`` at a NEW head SHA. The stored version must stay at the forced
    value (NOT advance to INDEX_SEMANTICS_VERSION) -- proving a zero-parse run
    cannot manufacture a spurious "current version" base case for the delta
    induction. The commit still advances (the run DID look at that SHA); only
    the version is held back. The FOLLOWING non-empty run must then full-path
    every file, since the branch is still stamped stale.
    """
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=_items(MAIN, UTIL))
    main_ids_before = _symbol_ids(conn, "main.py")
    conn.execute(text("UPDATE repo_branches SET index_semantics_version = 3"))
    conn.commit()

    counts = _index_default(conn, name="acme/widgets", head_sha="sha_zero", items=[])
    assert counts == IndexCounts(files=0, symbols=0, swept=0, edges=0)
    stamp = conn.execute(
        text(
            "SELECT rb.last_indexed_commit, rb.index_semantics_version FROM repo_branches rb "
            "JOIN repos r ON r.id = rb.repo_id WHERE r.name = 'acme/widgets'"
        )
    ).one()
    # last_indexed_commit DID advance (the run looked at sha_zero); the version
    # did NOT (nothing was indexed at it).
    assert stamp == ("sha_zero", 3)
    conn.rollback()

    # The next non-empty run sees baseline_version=3 != INDEX_SEMANTICS_VERSION,
    # so the gate is closed and every file takes the full path -- symbol ids
    # change even though the content is byte-identical to sha_first's.
    _index_default(conn, name="acme/widgets", head_sha="sha_second", items=_items(MAIN, UTIL))
    assert _symbol_ids(conn, "main.py") != main_ids_before
    stamp_after = conn.execute(
        text(
            "SELECT rb.index_semantics_version FROM repo_branches rb "
            "JOIN repos r ON r.id = rb.repo_id WHERE r.name = 'acme/widgets'"
        )
    ).scalar_one()
    assert stamp_after == INDEX_SEMANTICS_VERSION


# --- Test 23: the pre-read is index-served, not a Seq Scan ------------------


def _explain(
    conn: Connection, sql: str, params: dict[str, Any], *, analyze: bool = False
) -> dict[str, Any]:
    """``EXPLAIN (FORMAT JSON)`` for ``sql`` with the seq-scan escape hatch disabled,
    scoped to a SAVEPOINT so the ``enable_seqscan`` GUC change never leaks past this
    call (clones ``tests/integration/test_query_compiler.py``'s ``_explain_plan``
    idiom, per ``tests/integration/AGENTS.md``)."""
    mode = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
    savepoint = conn.begin_nested()
    try:
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        raw = conn.execute(text(f"EXPLAIN ({mode}) {sql}"), params).scalar_one()
    finally:
        savepoint.rollback()
    plan_list = json.loads(raw) if isinstance(raw, str) else raw
    plan: dict[str, Any] = plan_list[0]["Plan"]
    return plan


def _plan_nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield plan
    for child in plan.get("Plans", []) or []:
        yield from _plan_nodes(child)


@pytest.mark.integration
def test_the_delta_pre_read_is_index_served(conn: Connection) -> None:
    """Statement 3a (``branches @> ...``) plans an index scan on
    ``ix_files_branches_gin``; statement 3b (unqualified, repo-scoped) plans an
    Index Only Scan on ``uq_files_repo_path_sha`` with zero heap fetches --
    proving ``read_repo_content_shas`` never resorts to a Seq Scan and never
    pulls ``content`` off the heap. Seeded with 200 rows and ``VACUUM ANALYZE``d
    (an Index Only Scan additionally needs a set visibility map, which
    freshly-inserted, never-vacuumed rows do not have) so the plan is not a
    tiny-corpus degenerate choice -- see
    ``test_query_compiler.py::_explain_plan``'s docstring for that failure mode.

    ``VACUUM`` cannot run inside a transaction block, and SQLAlchemy 2.0
    autobegins one on every ``execute`` (a preceding ``conn.commit()`` does not
    help -- the next ``execute`` autobegins again), so it runs on a SEPARATE
    connection with ``execution_options(isolation_level="AUTOCOMMIT")``, never
    on this module's transaction-bound ``conn`` fixture.
    """
    items = _items(*[_fn("f", i) for i in range(200)])
    _index_default(conn, name="acme/widgets", head_sha="sha_first", items=items)
    repo_id = int(
        conn.execute(text("SELECT id FROM repos WHERE name = 'acme/widgets'")).scalar_one()
    )
    conn.commit()

    engine = conn.engine
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as vac_conn:
        vac_conn.execute(text(f"SET search_path TO {SCHEMA}, public"))
        vac_conn.execute(text("VACUUM ANALYZE files"))

    plan_a = _explain(
        conn,
        "SELECT path, content_sha FROM files "
        "WHERE repo_id = :repo_id AND branches @> CAST(:branch_arr AS text[])",
        {"repo_id": repo_id, "branch_arr": ["main"]},
    )
    a_nodes = list(_plan_nodes(plan_a))
    assert any(n.get("Index Name") == "ix_files_branches_gin" for n in a_nodes), a_nodes
    assert not any(n.get("Node Type") == "Seq Scan" for n in a_nodes), a_nodes
    a_output = " ".join(plan_a.get("Output") or [])
    assert "content" not in a_output and "files.content" not in a_output, plan_a

    plan_b = _explain(
        conn,
        "SELECT path, content_sha FROM files WHERE repo_id = :repo_id",
        {"repo_id": repo_id},
        analyze=True,
    )
    b_nodes = list(_plan_nodes(plan_b))
    assert not any(n.get("Node Type") == "Seq Scan" for n in b_nodes), b_nodes
    io_scan = next((n for n in b_nodes if n.get("Index Name") == "uq_files_repo_path_sha"), None)
    assert io_scan is not None, b_nodes
    assert io_scan.get("Node Type") == "Index Only Scan", io_scan
    assert io_scan.get("Heap Fetches") == 0, io_scan
    # The two expensive mistakes read_repo_content_shas' docstring names: the
    # Index Only Scan's own output list proves neither `content` nor `branches`
    # is ever fetched -- content_sha/path/repo_id are the constraint's own
    # columns, so an Index Only Scan is fundamentally incapable of returning
    # anything else.
    io_output = " ".join(io_scan.get("Output") or [])
    assert "content" not in io_output
    assert "branches" not in io_output
