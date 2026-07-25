"""Unit tests for indexer.bulk.insert_rows: statement shape and slicing, no DB required.

A fake ``Connection`` records the compiled ``Insert`` construct passed to
``execute`` so the multi-row-VALUES shape (as opposed to an executemany param
list) and the exact row count per statement can be asserted without a real
Postgres.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Column, Integer, MetaData, Table
from sqlalchemy.dialects.postgresql import dialect as pg_dialect

from indexer.bulk import insert_rows

_METADATA = MetaData()
_TABLE = Table("widgets", _METADATA, Column("a", Integer), Column("b", Integer))


class _FakeConn:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def execute(self, stmt: Any, params: Any = None) -> None:
        assert params is None  # rows travel inside stmt.values(...), not as a param list
        self.statements.append(stmt)


def _rows(n: int) -> list[dict[str, int]]:
    return [{"a": i, "b": i * 10} for i in range(n)]


@pytest.mark.unit
def test_empty_list_issues_zero_statements() -> None:
    conn = _FakeConn()
    insert_rows(conn, _TABLE, [])
    assert conn.statements == []


@pytest.mark.unit
def test_slicing_produces_exact_ceil_statements_every_row_once_in_order() -> None:
    # 2 columns, budget 10 -> 5 rows/statement. 12 rows -> ceil(12/5) = 3 statements
    # of sizes 5, 5, 2.
    conn = _FakeConn()
    rows = _rows(12)
    insert_rows(conn, _TABLE, rows, param_budget=10)

    assert len(conn.statements) == 3
    sizes = [len(stmt._multi_values[0]) for stmt in conn.statements]
    assert sizes == [5, 5, 2]

    seen: list[dict[str, int]] = []
    for stmt in conn.statements:
        seen.extend(stmt._multi_values[0])
    assert seen == rows  # every row present exactly once, in input order


@pytest.mark.unit
def test_no_statement_exceeds_budget_over_columns_rows() -> None:
    conn = _FakeConn()
    insert_rows(conn, _TABLE, _rows(37), param_budget=10)
    for stmt in conn.statements:
        assert len(stmt._multi_values[0]) <= 10 // 2


@pytest.mark.unit
def test_each_statement_is_one_multi_row_values_insert_not_executemany() -> None:
    # Compiling against the real PG dialect must yield one bind param per
    # (row, column) pair -- proof this is a single multi-row VALUES statement,
    # not something the driver could page into several round trips.
    conn = _FakeConn()
    insert_rows(conn, _TABLE, _rows(3), param_budget=10_000)
    assert len(conn.statements) == 1
    compiled = conn.statements[0].compile(dialect=pg_dialect())
    assert len(compiled.params) == 3 * 2


@pytest.mark.unit
def test_single_row_still_slices_to_one_statement() -> None:
    conn = _FakeConn()
    insert_rows(conn, _TABLE, _rows(1), param_budget=10)
    assert len(conn.statements) == 1
    assert len(conn.statements[0]._multi_values[0]) == 1
