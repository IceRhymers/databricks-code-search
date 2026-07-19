"""Unit tests for symbol search (issue #13): pure walker + rendered projection SQL.

No DB. The ``sym:`` atom walker and the step-2 projection builder are pure, so the walker is
asserted directly and the ``Select`` is rendered via ``stmt.compile(dialect=postgresql
.dialect())`` (mirrors the compiler suite's operator-fragment style). The full two-query
``symbol_search`` (compile candidates -> project) is exercised in the CI-only integration suite.
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from app.query.parser import parse
from app.search.symbols import _build_symbol_select, _collect_symbol_patterns


def _patterns(query: str) -> list[str]:
    out: list[str] = []
    _collect_symbol_patterns(parse(query), out)
    return out


# --------------------------------------------------------------------------- walker


@pytest.mark.unit
def test_collect_single_sym_atom() -> None:
    assert _patterns("sym:Handler") == ["Handler"]


@pytest.mark.unit
def test_collect_ignores_non_sym_leaves() -> None:
    # Substring, regex, repo/path/lang filters all contribute no symbol patterns.
    assert _patterns("repo:acme lang:go /Foo.*/ file:x bar") == []


@pytest.mark.unit
def test_collect_walks_and_or_nesting() -> None:
    # Both sym: atoms are collected regardless of the AND/OR the user wrote them under.
    assert _patterns("sym:Handler (sym:Parse OR lang:go)") == ["Handler", "Parse"]


@pytest.mark.unit
def test_collect_multiple_sym_atoms_union() -> None:
    assert _patterns("sym:Handler sym:Parse") == ["Handler", "Parse"]


# ------------------------------------------------------------------ projection SQL


def _render(
    file_ids: list[int], patterns: list[str], *, case_sensitive: bool = False, row_limit: int = 200
) -> str:
    stmt = _build_symbol_select(
        file_ids, patterns, case_sensitive=case_sensitive, row_limit=row_limit
    )
    return str(stmt.compile(dialect=postgresql.dialect()))


@pytest.mark.unit
def test_projection_selects_symbol_and_file_columns() -> None:
    sql = _render([1, 2], ["Handler"])
    assert "symbols.repo_id" in sql
    assert "files.path" in sql
    assert "files.lang" in sql
    assert "symbols.name" in sql
    assert "symbols.kind" in sql
    assert "symbols.start_line" in sql


@pytest.mark.unit
def test_projection_joins_files_and_bounds_by_file_ids() -> None:
    sql = _render([1, 2], ["Handler"])
    assert "JOIN files ON symbols.file_id = files.id" in sql
    # file eligibility is a concrete id list (no correlated subquery in the projection).
    assert "symbols.file_id IN" in sql


@pytest.mark.unit
def test_projection_name_operator_case_insensitive_default() -> None:
    sql = _render([1], ["Handler"], case_sensitive=False)
    assert "symbols.name ~*" in sql


@pytest.mark.unit
def test_projection_name_operator_case_sensitive() -> None:
    sql = _render([1], ["Handler"], case_sensitive=True)
    assert "symbols.name ~ " in sql
    assert "~*" not in sql


@pytest.mark.unit
def test_projection_multiple_patterns_are_ored() -> None:
    sql = _render([1], ["Handler", "Parse"])
    assert sql.count("symbols.name ~*") == 2
    assert " OR " in sql


@pytest.mark.unit
def test_projection_orders_deterministically_with_id_tiebreak() -> None:
    sql = _render([1], ["Handler"])
    order = sql.split("ORDER BY", 1)[1]
    # symbols has no natural uniqueness, so the id tiebreak makes the LIMIT page stable.
    assert "symbols.repo_id" in order
    assert "files.path" in order
    assert "symbols.start_line" in order
    assert "symbols.name" in order
    assert "symbols.id" in order


@pytest.mark.unit
def test_projection_applies_limit() -> None:
    sql = _render([1], ["Handler"], row_limit=50)
    assert "LIMIT" in sql
