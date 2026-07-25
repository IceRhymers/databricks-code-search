"""Unit tests for indexer.store's batched changed/new write path (#105), at the
STATEMENT level.

Complements ``test_store_delta.py`` (classification) and
``test_store_chunk_writer.py`` (chunk_writer wiring): this module pins the
batch FLUSH itself -- boundary tripping by count and by bytes, the
``excluded.*`` trap, id mapping by ``(path, content_sha)`` (never row order),
the intra-batch dedup guard, and the exact statement inventory of a mixed run.

Uses the same ``_FakeConn`` idiom as ``test_store_delta.py`` (a hand-rolled
fake ``Connection`` that labels each statement), extended with a
``files_returning``/``membership_returning`` hook so tests can script the
``RETURNING`` row order (or drop a row) independently of insertion order --
the whole point of tests 7-9 below.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Delete, Insert, Update
from sqlalchemy.dialects.postgresql import dialect as pg_dialect

import indexer.store as store_module
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


class _IdRow(NamedTuple):
    """The batched ``files``/membership-union ``RETURNING id, path, content_sha`` shape."""

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
        self._rows = rows if rows is not None else []
        self.rowcount = rowcount

    def scalar_one(self) -> Any:
        return self._scalar

    def one(self) -> Any:
        return self._row

    def all(self) -> list[Any]:
        return self._rows

    def __iter__(self) -> Any:
        return iter(self._rows)


class _FakeConn:
    def __init__(
        self,
        *,
        baseline: tuple[str | None, int | None] = (None, None),
        carried: set[tuple[str, str]] | None = None,
        present: set[tuple[str, str]] | None = None,
        provenance: bool = True,
        stamp_rowcount: int = 1,
        files_returning: Callable[[list[dict[str, Any]]], list[_IdRow]] | None = None,
        membership_returning: Callable[[dict[str, Any]], list[_IdRow]] | None = None,
    ) -> None:
        self._next_file_id = 1
        self._baseline = baseline
        self._carried = sorted(carried or set())
        self._present = sorted(present or set())
        self._provenance = provenance
        self._stamp_rowcount = stamp_rowcount
        self._files_returning = files_returning
        self._membership_returning = membership_returning
        self.kinds: list[str] = []
        self.files_upsert_stmts: list[Any] = []
        self.symbol_inserts: list[list[dict[str, Any]]] = []
        self.edge_inserts: list[list[dict[str, Any]]] = []
        self.delete_calls: list[tuple[str, list[int]]] = []
        self.membership_params: dict[str, Any] = {}
        self.stamp_values: dict[str, Any] = {}

    def begin(self) -> Any:
        return contextlib.nullcontext()

    def _text_execute(self, sql: str, params: Any) -> _FakeResult:
        if "SELECT path, content_sha FROM files" in sql and "branches @>" in sql:
            self.kinds.append("read-carried")
            return _FakeResult(rows=[_IdRow(0, p, s) for p, s in self._carried])
        if "SELECT path, content_sha FROM files" in sql:
            self.kinds.append("read-present")
            return _FakeResult(rows=[_IdRow(0, p, s) for p, s in self._present])
        if "SELECT NOT EXISTS" in sql:
            self.kinds.append("provenance-gate")
            return _FakeResult(scalar=self._provenance)
        if "UPDATE files" in sql and "array_agg(DISTINCT e)" in sql:
            self.kinds.append("membership-union")
            self.membership_params = dict(params or {})
            if self._membership_returning is not None:
                rows = self._membership_returning(params or {})
            else:
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
            self.kinds.append("files-upsert-batch")
            self.files_upsert_stmts.append(stmt)
            rows_in = stmt._multi_values[0]
            if self._files_returning is not None:
                out = self._files_returning(list(rows_in))
            else:
                out = []
                for row in rows_in:
                    file_id = self._next_file_id
                    self._next_file_id += 1
                    out.append(_IdRow(file_id, row["path"], row["content_sha"]))
            return _FakeResult(rows=out)
        if isinstance(stmt, Delete) and table == "symbols":
            self.kinds.append("symbols-delete")
            self.delete_calls.append(("symbols", list((params or {}).get("ids", []))))
            return _FakeResult()
        if isinstance(stmt, Insert) and table == "symbols":
            self.kinds.append("symbols-insert")
            self.symbol_inserts.append(list(stmt._multi_values[0]))
            return _FakeResult()
        if isinstance(stmt, Delete) and table == "reference_edges":
            self.kinds.append("edges-delete")
            self.delete_calls.append(("reference_edges", list((params or {}).get("ids", []))))
            return _FakeResult()
        if isinstance(stmt, Insert) and table == "reference_edges":
            self.kinds.append("edges-insert")
            self.edge_inserts.append(list(stmt._multi_values[0]))
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


# --- Test 3: transaction shape pinned across every batching configuration ----


@pytest.mark.unit
@pytest.mark.parametrize("batch_max_files", [1, 2, 7, 500])
def test_transaction_shape_is_pinned_across_batch_sizes(
    monkeypatch: pytest.MonkeyPatch, batch_max_files: int
) -> None:
    """T3: repos first, repo_branches second, stamp LAST, sweep immediately
    before the stamp -- whatever the batch size."""
    monkeypatch.setattr(store_module, "_BATCH_MAX_FILES", batch_max_files)
    items = [_item(f"f{i}.py", f"x = {i}\n") for i in range(3)]
    conn = _FakeConn()
    _index(conn, items)

    assert conn.kinds[0] == "repos-insert"
    assert conn.kinds[1] == "repo-branches-insert"
    assert conn.kinds[-1] == "stamp"
    assert conn.kinds[-3:-1] == ["sweep-update", "sweep-delete"]


# --- Test 4: batch boundary by count ------------------------------------------


@pytest.mark.unit
def test_batch_boundary_by_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """T4: 1001 changed files at _BATCH_MAX_FILES=500 -> 3 files-upsert-batch
    statements of sizes 500/500/1, the third being the post-loop flush."""
    monkeypatch.setattr(store_module, "_BATCH_MAX_FILES", 500)
    items = [_item(f"f{i}.py", f"x = {i}\n") for i in range(1001)]
    conn = _FakeConn()
    _index(conn, items)

    sizes = [len(stmt._multi_values[0]) for stmt in conn.files_upsert_stmts]
    assert sizes == [500, 500, 1]


# --- Test 5: excluded.*, not per-file literals --------------------------------


@pytest.mark.unit
def test_files_upsert_set_clause_uses_excluded_never_a_literal() -> None:
    """T5: the SET clause of the batched upsert must bind lang/size/content/commit
    via excluded.*, never a Python literal from one file -- the trap that would
    make every conflicting row in a batch collapse to the LAST file's values."""
    conn = _FakeConn()
    _index(conn, [_item("a.py", "x = 1\n")])

    stmt = conn.files_upsert_stmts[0]
    sql = str(stmt.compile(dialect=pg_dialect()))
    set_clause = sql.split("DO UPDATE SET", 1)[1].split("RETURNING", 1)[0]

    assert "excluded.lang" in set_clause
    assert "excluded.size" in set_clause
    assert "excluded.content" in set_clause
    assert "excluded.commit" in set_clause
    # No bound literal anywhere in the SET clause -- only the VALUES(...)
    # portion (checked separately, not here) may carry bind params.
    assert "%(" not in set_clause


# --- Test 6: batch boundary by bytes ------------------------------------------


@pytest.mark.unit
def test_batch_boundary_by_bytes_flushes_early_and_never_splits_a_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T6: the byte bound flushes early (post-append check); a single file
    individually larger than the bound is neither dropped nor split -- it
    flushes with whatever batch it landed in."""
    monkeypatch.setattr(store_module, "_BATCH_MAX_CONTENT_BYTES", 10)
    monkeypatch.setattr(store_module, "_BATCH_MAX_FILES", 500)
    items = [
        _item("a.py", "aaaaaa"),  # 6 bytes
        _item("b.py", "bbbbbb"),  # 6 bytes -> batch now 12 >= 10, flushes (2 files)
        _item("c.py", "c" * 20),  # 20 bytes alone, over the bound -- still 1 batch
    ]
    conn = _FakeConn()
    _index(conn, items)

    sizes = [len(stmt._multi_values[0]) for stmt in conn.files_upsert_stmts]
    assert sizes == [2, 1]


# --- Test 7: chunk_writer once per batch, ids mapped by (path, sha) ----------


@pytest.mark.unit
def test_chunk_writer_called_once_per_batch_with_shuffled_returning_order() -> None:
    """T7: ONE chunk_writer call per batch, with ids taken from RETURNING mapped
    by (path, content_sha) -- proven by deliberately returning RETURNING rows
    in the REVERSE of insertion order."""
    calls: list[list[tuple[int, ParsedFile]]] = []

    def chunk_writer(conn: Any, repo_id: int, pairs: Any) -> None:
        calls.append(list(pairs))

    def shuffled_returning(rows_in: list[dict[str, Any]]) -> list[_IdRow]:
        out = []
        next_id = 100
        for row in reversed(rows_in):
            out.append(_IdRow(next_id, row["path"], row["content_sha"]))
            next_id += 1
        return out

    conn = _FakeConn(files_returning=shuffled_returning)
    items = [_item("a.py", "x = 1\n"), _item("b.py", "y = 2\n"), _item("c.py", "z = 3\n")]
    _index(conn, items, chunk_writer=chunk_writer)

    assert len(calls) == 1
    got = {pf.path: file_id for file_id, pf in calls[0]}
    # Returned in reverse -> c.py got the FIRST id minted, a.py the LAST.
    assert got == {"c.py": 100, "b.py": 101, "a.py": 102}


# --- Test 8: a missing RETURNING row raises, no further statement issues ----


@pytest.mark.unit
def test_missing_returning_row_raises_and_issues_no_further_statement() -> None:
    """T8: an id missing from RETURNING must RAISE, never warn-and-skip -- a
    wrong or silently-dropped file_id here would attach one file's symbols to
    another file's row. No delete/insert/stamp follows the failed upsert."""

    def drop_first(rows_in: list[dict[str, Any]]) -> list[_IdRow]:
        out = []
        next_id = 1
        for row in rows_in[1:]:
            out.append(_IdRow(next_id, row["path"], row["content_sha"]))
            next_id += 1
        return out

    conn = _FakeConn(files_returning=drop_first)
    items = [_item("a.py", "x = 1\n"), _item("b.py", "y = 2\n")]
    with pytest.raises(RuntimeError, match="a.py"):
        _index(conn, items)

    assert "symbols-delete" not in conn.kinds
    assert "stamp" not in conn.kinds


# --- Test 9: membership row-vanished filter drops just that pair -------------


@pytest.mark.unit
def test_membership_vanished_row_drops_only_that_pair_from_the_chunk_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T9: the membership seam is already called once with the whole list
    (#105 commit 3); a row missing from the union's RETURNING drops only that
    one pair (WARNING), the rest of the batch's chunk_writer call is unaffected."""
    calls: list[list[tuple[int, ParsedFile]]] = []

    def chunk_writer(conn: Any, repo_id: int, pairs: Any) -> None:
        calls.append(list(pairs))

    def only_b(params: dict[str, Any]) -> list[_IdRow]:
        # a.py "vanished" between the projection read and the union -- only
        # b.py's row comes back from RETURNING.
        return [_IdRow(1, "b.py", content_sha("y = 2\n"))]

    items = [_item("a.py", "x = 1\n"), _item("b.py", "y = 2\n")]
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried=set(),
        present={_key("a.py", "x = 1\n"), _key("b.py", "y = 2\n")},
        membership_returning=only_b,
    )
    with caplog.at_level(logging.WARNING, logger="indexer.store"):
        _index(conn, items, chunk_writer=chunk_writer)

    assert len(calls) == 1
    assert [pf.path for _fid, pf in calls[0]] == ["b.py"]
    assert any("a.py" in r.getMessage() and "vanished" in r.getMessage() for r in caplog.records)


# --- Test 10: intra-batch duplicate (path, content_sha) collapses -----------


@pytest.mark.unit
def test_intra_batch_duplicate_collapses_to_last_wins_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T10: two items sharing (path, content_sha) within one batch collapse to
    ONE upsert row (last occurrence wins, matching this module's per-file
    last-write-wins) and log exactly one WARNING -- the guard against the
    verified ON CONFLICT cardinality-violation."""
    dup_first = _item("a.py", "x = 1\n", symbols=True)
    dup_last = _item("a.py", "x = 1\n", symbols=False)  # same (path, sha)
    conn = _FakeConn()
    with caplog.at_level(logging.WARNING, logger="indexer.store"):
        counts = _index(conn, [dup_first, dup_last])

    assert conn.kinds.count("files-upsert-batch") == 1
    assert len(conn.files_upsert_stmts[0]._multi_values[0]) == 1
    duplicate_warnings = [r for r in caplog.records if "duplicate" in r.getMessage()]
    assert len(duplicate_warnings) == 1
    # Last occurrence wins: dup_last has no symbols.
    assert counts.symbols == 0


# --- Test 11: exact statement inventory over a mixed run --------------------


@pytest.mark.unit
def test_full_statement_inventory_mixed_run_semantic_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T11 (AC1, unit-tier): 1 unchanged, 1 membership-only, 1 changed
    (with edges), 1 new -> the exact inventory. moved.py and new.py flush
    TOGETHER in one batch (default _BATCH_MAX_FILES). IndexCounts and the
    `delta write set` INFO line are unaffected by batching."""
    unchanged = _item("keep.py", "k = 1\n")
    member = _item("shared.py", "s = 1\n")
    changed = _item("moved.py", "m = 2\n", edges=True)
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
    with caplog.at_level(logging.INFO, logger="indexer.store"):
        counts = _index(conn, [unchanged, member, changed, added])

    assert conn.kinds == [
        "repos-insert",
        "repo-branches-insert",
        "read-carried",
        "read-present",
        "provenance-gate",
        "files-upsert-batch",
        "symbols-delete",
        "symbols-insert",
        "edges-delete",
        "edges-insert",
        "membership-union",
        "sweep-update",
        "sweep-delete",
        "stamp",
    ]
    assert counts == IndexCounts(files=4, symbols=2, swept=0, edges=1)
    assert (
        "acme/widgets@main: delta write set 2/4 files "
        "(unchanged=1 membership=1, semantics gate open)"
    ) in caplog.text


@pytest.mark.unit
def test_full_statement_inventory_mixed_run_semantic_on() -> None:
    """T11, semantic-on half: ONE chunk_writer call for the changed/new batch,
    a SEPARATE one for the membership class -- never per file."""
    chunk_calls: list[list[str]] = []

    def chunk_writer(conn: Any, repo_id: int, pairs: Any) -> None:
        chunk_calls.append([pf.path for _fid, pf in pairs])

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
    _index(conn, [unchanged, member, changed, added], chunk_writer=chunk_writer)

    assert chunk_calls == [["moved.py", "new.py"], ["shared.py"]]


@pytest.mark.unit
def test_empty_membership_within_a_mixed_run_still_skips_the_union() -> None:
    """T11, stability half: an empty membership class is still skipped even in
    a run that also has a changed/new batch -- keeps the inventories above
    stable regardless of what else the run wrote."""
    changed = _item("moved.py", "m = 2\n")
    conn = _FakeConn(
        baseline=("sha_old", INDEX_SEMANTICS_VERSION),
        carried={_key("moved.py", "m = 1\n")},
        present={_key("moved.py", "m = 1\n")},
    )
    _index(conn, [changed])
    assert "membership-union" not in conn.kinds


# --- The libpq bind-param invariant -------------------------------------------


@pytest.mark.unit
def test_batch_max_files_times_columns_is_under_the_bind_param_ceiling() -> None:
    """libpq's Bind message carries the parameter count in an int16, so no
    single statement may bind more than 65535 params."""
    assert store_module._BATCH_MAX_FILES * store_module._FILE_UPSERT_COLUMNS < 65535
