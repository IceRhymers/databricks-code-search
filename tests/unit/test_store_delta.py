"""Unit tests for ``index_repo``'s file-level delta path, at the STATEMENT level.

A hand-rolled fake ``Connection`` (the ``_FakeConn`` idiom from
``tests/unit/test_store_chunk_writer.py``, extended to answer the two projection
reads and the provenance gate) stands in for Postgres, so these are true unit
tests: what they pin is the exact *statement inventory* each classification
produces, and the order the transaction issues them in. Row-identity proof --
that skipping really does leave the stored serials untouched -- is
``tests/integration/test_store_delta.py``'s job, against real SQL.

The fake records one short label per executed statement (``repos-insert``,
``read-carried``, ``file-upsert``, ...) rather than the statement objects, so an
assertion reads as the inventory it is checking.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Delete, Insert, Update

from app.db.models import INDEX_SEMANTICS_VERSION
from indexer.hashing import content_sha
from indexer.languages import (
    ExtractedEdge,
    ExtractedSymbol,
    FileExtraction,
    IndexCounts,
    ParsedFile,
)
from indexer.store import index_repo


class _ShaRow(NamedTuple):
    """The exact projection statements 3a/3b return -- path and content_sha, nothing else."""

    path: str
    content_sha: str


class _IdRow(NamedTuple):
    """The batched membership ``UPDATE ... RETURNING id, path, content_sha`` shape."""

    id: int
    path: str
    content_sha: str


class _FakeResult:
    def __init__(
        self,
        *,
        scalar: Any = None,
        rowcount: int = 0,
        row: Any = None,
        rows: list[Any] | None = None,
    ) -> None:
        self._scalar = scalar
        self._row = row
        self._rows = rows or []
        self.rowcount = rowcount

    def scalar_one(self) -> Any:
        return self._scalar

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def one(self) -> Any:
        return self._row

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeConn:
    """Answers every statement ``index_repo`` can issue, and labels it.

    ``baseline`` is what statement 2's ``RETURNING`` yields -- the pair that
    opens or closes the delta gate. ``carried``/``present`` script statements 3a
    and 3b (see ``indexer.store.read_repo_content_shas``); ``provenance``
    scripts statement 4's ``NOT EXISTS`` gate.
    """

    def __init__(
        self,
        *,
        baseline: tuple[str | None, int | None] = (None, None),
        carried: set[tuple[str, str]] | None = None,
        present: set[tuple[str, str]] | None = None,
        provenance: bool = True,
        stamp_rowcount: int = 1,
    ) -> None:
        self._next_file_id = 1
        self._baseline = baseline
        self._carried = sorted(carried or set())
        self._present = sorted(present or set())
        self._provenance = provenance
        self._stamp_rowcount = stamp_rowcount
        self.kinds: list[str] = []
        self.stamp_values: dict[str, Any] = {}
        self.membership_params: dict[str, Any] = {}

    def begin(self) -> Any:
        return contextlib.nullcontext()

    def _text_execute(self, sql: str, params: Any) -> _FakeResult:
        # Ordered most-specific-first: statement 3b's text is a PREFIX of 3a's.
        if "SELECT path, content_sha FROM files" in sql and "branches @>" in sql:
            self.kinds.append("read-carried")
            return _FakeResult(rows=[_ShaRow(p, s) for p, s in self._carried])
        if "SELECT path, content_sha FROM files" in sql:
            self.kinds.append("read-present")
            return _FakeResult(rows=[_ShaRow(p, s) for p, s in self._present])
        if "SELECT NOT EXISTS" in sql:
            self.kinds.append("provenance-gate")
            return _FakeResult(scalar=self._provenance)
        if "UPDATE files" in sql and "array_agg(DISTINCT e)" in sql:
            self.kinds.append("membership-union")
            self.membership_params = dict(params or {})
            rows = []
            for path, sha in zip(
                (params or {}).get("paths", []), (params or {}).get("shas", []), strict=True
            ):
                rows.append(_IdRow(self._next_file_id, path, sha))
                self._next_file_id += 1
            return _FakeResult(rows=rows, rowcount=len(rows))
        if "UPDATE files SET branches = array_remove" in sql:
            self.kinds.append("sweep-update")
            return _FakeResult(rowcount=0)
        if "DELETE FROM files" in sql:
            self.kinds.append("sweep-delete")
            return _FakeResult(rowcount=0)
        raise AssertionError(f"unexpected text() statement: {sql!r}")

    def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = getattr(stmt, "text", None)
        if sql is not None:
            return self._text_execute(sql, params)

        table = stmt.table.name
        if isinstance(stmt, Insert) and table == "repos":
            self.kinds.append("repos-insert")
            return _FakeResult(scalar=1)
        if isinstance(stmt, Insert) and table == "repo_branches":
            self.kinds.append("repo-branches-insert")
            return _FakeResult(row=self._baseline)
        if isinstance(stmt, Insert) and table == "files":
            self.kinds.append("file-upsert")
            file_id = self._next_file_id
            self._next_file_id += 1
            return _FakeResult(scalar=file_id)
        if isinstance(stmt, Delete) and table == "symbols":
            self.kinds.append("symbols-delete")
            return _FakeResult()
        if isinstance(stmt, Insert) and table == "symbols":
            self.kinds.append("symbols-insert")
            return _FakeResult()
        if isinstance(stmt, Delete) and table == "reference_edges":
            self.kinds.append("edges-delete")
            return _FakeResult()
        if isinstance(stmt, Insert) and table == "reference_edges":
            self.kinds.append("edges-insert")
            return _FakeResult()
        if isinstance(stmt, Update) and table == "repo_branches":
            self.kinds.append("stamp")
            self.stamp_values = dict(stmt.compile().params)
            return _FakeResult(rowcount=self._stamp_rowcount)
        raise AssertionError(f"unexpected statement against {table!r}: {stmt}")


def _pf(path: str, content: str) -> ParsedFile:
    return ParsedFile(path=path, lang="python", size=len(content.encode()), content=content)


def _item(
    path: str, content: str, *, symbols: bool = True, edges: bool = False
) -> tuple[ParsedFile, FileExtraction]:
    symbol = ExtractedSymbol("f", "function", 1, 2)
    return (
        _pf(path, content),
        FileExtraction(
            symbols=[symbol] if symbols else [],
            edges=[ExtractedEdge(kind="call", target="t", line=2, enclosing=symbol)]
            if edges
            else [],
        ),
    )


def _key(path: str, content: str) -> tuple[str, str]:
    return (path, content_sha(content))


def _index(conn: _FakeConn, items: Any, **kwargs: Any) -> IndexCounts:
    return index_repo(
        conn,
        name="acme/widgets",
        branch="main",
        is_default=True,
        head_sha="sha_new",
        items=items,
        **kwargs,
    )


# --- The gate: closed unless the stored version equals the current one --------


@pytest.mark.unit
@pytest.mark.parametrize(
    "baseline",
    [(None, None), ("sha_old", None), ("sha_old", INDEX_SEMANTICS_VERSION - 1)],
    ids=["never-indexed", "null-version", "older-version"],
)
def test_version_mismatch_never_issues_the_projection_reads(
    baseline: tuple[str | None, int | None],
) -> None:
    """T1: a NULL or stale stored version means full path for every file, and the
    two projection reads (and the provenance gate) are never issued at all."""
    conn = _FakeConn(baseline=baseline, carried={_key("a.py", "x = 1\n")})
    counts = _index(conn, [_item("a.py", "x = 1\n")])

    assert "read-carried" not in conn.kinds
    assert "read-present" not in conn.kinds
    assert "provenance-gate" not in conn.kinds
    assert "file-upsert" in conn.kinds
    assert counts == IndexCounts(files=1, symbols=1, swept=0, edges=0)


@pytest.mark.unit
def test_delta_on_all_unchanged_writes_nothing() -> None:
    """T2 (AC1): every parsed file already carried by this branch at this content
    means zero files/symbols/reference_edges statements -- but the sweep still
    runs against the FULL seen-set, and the stamp is still last."""
    items = [_item("a.py", "x = 1\n"), _item("b.py", "y = 2\n")]
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried={_key("a.py", "x = 1\n"), _key("b.py", "y = 2\n")},
        present={_key("a.py", "x = 1\n"), _key("b.py", "y = 2\n")},
    )
    counts = _index(conn, items)

    assert conn.kinds == [
        "repos-insert",
        "repo-branches-insert",
        "read-carried",
        "read-present",
        "provenance-gate",
        "sweep-update",
        "sweep-delete",
        "stamp",
    ]
    # files still counts files SEEN this run (the seen-set size the sweep uses),
    # so the sweep and its empty-seen-set guard need no delta awareness.
    assert counts == IndexCounts(files=2, symbols=0, swept=0, edges=0)


@pytest.mark.unit
def test_all_unchanged_run_calls_no_chunk_writer() -> None:
    """T2 (AC1), chunk half: an unchanged file's chunk rows are never rewritten."""
    calls: list[str] = []
    items = [_item("a.py", "x = 1\n")]
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried={_key("a.py", "x = 1\n")},
        present={_key("a.py", "x = 1\n")},
    )
    _index(
        conn,
        items,
        chunk_writer=lambda _c, _r, pairs: calls.extend(pf.path for _fid, pf in pairs),
    )
    assert calls == []


@pytest.mark.unit
def test_changed_content_at_a_known_path_takes_the_full_path() -> None:
    """A path whose content moved is NOT in ``carried`` under its new sha, so it
    takes the full write path -- the (path, content_sha) keying, not path alone."""
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried={_key("a.py", "x = 1\n")},
        present={_key("a.py", "x = 1\n")},
    )
    counts = _index(conn, [_item("a.py", "x = 999\n", edges=True)])

    assert conn.kinds.count("file-upsert") == 1
    assert conn.kinds.count("symbols-delete") == 1
    assert conn.kinds.count("edges-delete") == 1
    assert counts == IndexCounts(files=1, symbols=1, swept=0, edges=1)


# --- Membership-only: one batched union, a chunk write, no symbol/edge work ---


@pytest.mark.unit
def test_membership_only_issues_one_batched_union_and_no_symbol_work() -> None:
    """T3 (AC3): a file stored for this repo but not carried by this branch takes
    the membership path -- ONE batched UPDATE for the whole class, ONE
    chunk_writer call carrying every file's (file_id, pf) pair, and no
    symbols/reference_edges statements."""
    chunk_writer_calls: list[list[tuple[int, str]]] = []
    items = [_item("a.py", "x = 1\n"), _item("b.py", "y = 2\n")]
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried=set(),
        present={_key("a.py", "x = 1\n"), _key("b.py", "y = 2\n")},
    )
    counts = _index(
        conn,
        items,
        chunk_writer=lambda _c, _r, pairs: chunk_writer_calls.append(
            [(fid, pf.path) for fid, pf in pairs]
        ),
    )

    assert conn.kinds.count("membership-union") == 1
    assert "file-upsert" not in conn.kinds
    assert "symbols-delete" not in conn.kinds
    assert "edges-delete" not in conn.kinds
    # ONE chunk_writer call for the whole membership class, and the file_id each
    # pair carries came from the UPDATE's RETURNING, not a second lookup.
    assert chunk_writer_calls == [[(1, "a.py"), (2, "b.py")]]
    assert conn.membership_params["paths"] == ["a.py", "b.py"]
    assert conn.membership_params["branch_arr"] == ["main"]
    # symbols/edges legitimately fall to zero: no rows were inserted.
    assert counts == IndexCounts(files=2, symbols=0, swept=0, edges=0)


@pytest.mark.unit
def test_membership_only_is_refused_when_a_sibling_branch_is_stale() -> None:
    """T4 (AC6): statement 4 false -- some branch of this repo sits at another
    semantics version -- forces every would-be membership file down the full
    path, so it can never inherit stale-version symbols/edges."""
    items = [_item("a.py", "x = 1\n")]
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried=set(),
        present={_key("a.py", "x = 1\n")},
        provenance=False,
    )
    counts = _index(conn, items)

    assert "provenance-gate" in conn.kinds
    assert "membership-union" not in conn.kinds
    assert conn.kinds.count("file-upsert") == 1
    assert conn.kinds.count("symbols-delete") == 1
    assert counts == IndexCounts(files=1, symbols=1, swept=0, edges=0)


@pytest.mark.unit
def test_mixed_classification_statement_inventory() -> None:
    """T5 (AC2): 1 unchanged, 1 membership-only, 1 changed, 1 new -> the exact
    inventory, with the batched union issued once, AFTER the per-file loop."""
    unchanged = _item("keep.py", "k = 1\n")
    member = _item("shared.py", "s = 1\n")
    changed = _item("moved.py", "m = 2\n")
    added = _item("new.py", "n = 1\n")
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried={_key("keep.py", "k = 1\n"), _key("moved.py", "m = 1\n")},
        present={
            _key("keep.py", "k = 1\n"),
            _key("moved.py", "m = 1\n"),
            _key("shared.py", "s = 1\n"),
        },
    )
    counts = _index(conn, [unchanged, member, changed, added])

    assert conn.kinds == [
        "repos-insert",
        "repo-branches-insert",
        "read-carried",
        "read-present",
        "provenance-gate",
        # moved.py -- changed content at a known path
        "file-upsert",
        "symbols-delete",
        "symbols-insert",
        "edges-delete",
        # new.py -- never seen
        "file-upsert",
        "symbols-delete",
        "symbols-insert",
        "edges-delete",
        # shared.py -- the whole membership class, batched, after the loop
        "membership-union",
        "sweep-update",
        "sweep-delete",
        "stamp",
    ]
    assert conn.membership_params["paths"] == ["shared.py"]
    assert counts == IndexCounts(files=4, symbols=2, swept=0, edges=0)


@pytest.mark.unit
def test_empty_membership_class_issues_no_union_statement() -> None:
    """T5, the stability half: an empty membership set is SKIPPED, never issued
    as a no-op UPDATE -- which is what keeps the inventories above stable."""
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried={_key("a.py", "x = 1\n")},
        present={_key("a.py", "x = 1\n")},
    )
    _index(conn, [_item("a.py", "x = 1\n")])
    assert "membership-union" not in conn.kinds


@pytest.mark.unit
def test_membership_without_a_chunk_writer_issues_only_the_union() -> None:
    """The semantic-off path: the union still runs, nothing else does."""
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried=set(),
        present={_key("a.py", "x = 1\n")},
    )
    _index(conn, [_item("a.py", "x = 1\n")], chunk_writer=None)
    assert conn.kinds.count("membership-union") == 1


# --- The `delta write set` line: always emitted, one shape -------------------


@pytest.mark.unit
def test_delta_write_set_line_reports_the_breakdown_with_the_gate_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T7: the breakdown rides its OWN line from indexer.store -- IndexCounts is
    unchanged, so this is where unchanged/membership become visible."""
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried={_key("keep.py", "k = 1\n")},
        present={_key("keep.py", "k = 1\n"), _key("shared.py", "s = 1\n")},
    )
    with caplog.at_level(logging.INFO, logger="indexer.store"):
        _index(
            conn,
            [
                _item("keep.py", "k = 1\n"),
                _item("shared.py", "s = 1\n"),
                _item("new.py", "n = 1\n"),
            ],
        )

    assert (
        "acme/widgets@main: delta write set 1/3 files "
        "(unchanged=1 membership=1, semantics gate open)"
    ) in caplog.text


@pytest.mark.unit
def test_delta_write_set_line_is_present_with_the_gate_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T7, the other half: the line never disappears -- a closed gate reports the
    full-path reason instead, so no grep an operator writes breaks."""
    conn = _FakeConn(baseline=("sha_old", INDEX_SEMANTICS_VERSION - 1))
    with caplog.at_level(logging.INFO, logger="indexer.store"):
        _index(conn, [_item("a.py", "x = 1\n"), _item("b.py", "y = 2\n")])

    assert (
        f"acme/widgets@main: delta write set 2/2 files (unchanged=0 membership=0, "
        f"semantics gate closed: stored v{INDEX_SEMANTICS_VERSION - 1} "
        f"!= v{INDEX_SEMANTICS_VERSION})"
    ) in caplog.text


# --- Transaction shape and the untouched guards ------------------------------


@pytest.mark.unit
def test_transaction_shape_is_pinned() -> None:
    """T6: the epic's non-negotiable rule. repos first, repo_branches second, the
    projection reads third, the CAS stamp LAST -- whatever the classification."""
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried={_key("a.py", "x = 1\n")},
        present={_key("a.py", "x = 1\n"), _key("b.py", "y = 2\n")},
    )
    _index(conn, [_item("a.py", "x = 1\n"), _item("c.py", "z = 3\n")])

    assert conn.kinds[0] == "repos-insert"
    assert conn.kinds[1] == "repo-branches-insert"
    assert conn.kinds[2] == "read-carried"
    assert conn.kinds[3] == "read-present"
    assert conn.kinds[4] == "provenance-gate"
    assert conn.kinds[-1] == "stamp"


@pytest.mark.unit
def test_empty_items_still_skips_the_sweep_with_delta_on() -> None:
    """T8: the empty-seen-set guard is untouched by the delta path."""
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried={_key("a.py", "x = 1\n")},
        present={_key("a.py", "x = 1\n")},
    )
    counts = _index(conn, [])

    assert "sweep-update" not in conn.kinds
    assert "sweep-delete" not in conn.kinds
    assert counts == IndexCounts(files=0, symbols=0, swept=0, edges=0)


@pytest.mark.unit
def test_zero_parse_run_does_not_advance_the_semantics_version() -> None:
    """The §2.3 base case, at the statement level: an empty seen-set stamps the
    commit but leaves ``index_semantics_version`` at the statement-2 baseline."""
    conn = _FakeConn(baseline=("sha_old", INDEX_SEMANTICS_VERSION - 1))
    _index(conn, [])

    assert conn.stamp_values["last_indexed_commit"] == "sha_new"
    assert conn.stamp_values["index_semantics_version"] == INDEX_SEMANTICS_VERSION - 1


@pytest.mark.unit
def test_non_empty_run_advances_the_semantics_version() -> None:
    """The complement: a run that wrote something DOES claim the current version."""
    conn = _FakeConn(baseline=("sha_old", INDEX_SEMANTICS_VERSION - 1))
    _index(conn, [_item("a.py", "x = 1\n")])

    assert conn.stamp_values["last_indexed_commit"] == "sha_new"
    assert conn.stamp_values["index_semantics_version"] == INDEX_SEMANTICS_VERSION
