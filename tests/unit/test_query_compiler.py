"""Unit tests for the query compiler (issue #9): AST -> SQLAlchemy select.

Pure, no DB. Each compiled statement is rendered via ``stmt.compile(dialect=postgresql
.dialect())`` and asserted on operator fragments in the SQL text plus bound parameters
(mirrors the parser suite's precise, parametrized style; avoids brittle full-string eq).
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from app.query.compiler import DEFAULT_ROW_LIMIT, compile_query
from app.query.parser import parse, resolve_case


def _render(query: str, **kwargs: object) -> tuple[str, dict[str, object]]:
    """Compile ``query`` and return (SQL text, bound params)."""
    stmt = compile_query(parse(query), **kwargs)  # type: ignore[arg-type]
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def _param_values(params: dict[str, object]) -> set[object]:
    """Bound param values excluding the LIMIT param."""
    return {v for k, v in params.items() if not k.startswith("param_")}


# ------------------------------------------------------------------------- substring


@pytest.mark.unit
def test_substring_is_ilike_case_insensitive() -> None:
    sql, params = _render("foo")
    assert "files.content ILIKE" in sql
    assert "LIKE" in sql  # ILIKE contains LIKE; ensure not a regex op
    assert "%foo%" in _param_values(params)
    assert "ESCAPE '\\\\'" in sql


@pytest.mark.unit
def test_case_yes_substring_is_like_not_ilike() -> None:
    sql, params = _render("case:yes foo")
    assert "files.content LIKE" in sql
    assert "ILIKE" not in sql
    assert "%foo%" in _param_values(params)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("query", "expected_param"),
    [
        ("foo_bar", "%foo\\_bar%"),  # underscore escaped
        ("50%", "%50\\%%"),  # percent escaped
        ("a\\b", "%a\\\\b%"),  # backslash escaped first
    ],
)
def test_substring_like_escaping(query: str, expected_param: str) -> None:
    sql, params = _render(query)
    assert expected_param in _param_values(params)
    assert "ESCAPE '\\\\'" in sql


# ----------------------------------------------------------------------------- regex


@pytest.mark.unit
def test_regex_is_case_insensitive_operator_raw_pattern() -> None:
    sql, params = _render("/foo.*bar/")
    assert "files.content ~*" in sql
    # Raw pattern, no LIKE escaping applied.
    assert "foo.*bar" in _param_values(params)


@pytest.mark.unit
def test_case_yes_regex_is_case_sensitive_operator() -> None:
    sql, params = _render("case:yes /foo/")
    assert "files.content ~ " in sql
    assert "~*" not in sql
    assert "foo" in _param_values(params)


# ---------------------------------------------------------------------------- filters


@pytest.mark.unit
def test_repo_filter_is_in_subquery_always_insensitive() -> None:
    sql, params = _render("repo:acme")
    assert "files.repo_id IN (SELECT repos.id" in sql
    assert "repos.name ~*" in sql
    assert "acme" in _param_values(params)


@pytest.mark.unit
def test_repo_filter_stays_insensitive_under_case_yes() -> None:
    sql, _ = _render("case:yes repo:acme")
    assert "repos.name ~*" in sql
    assert "repos.name ~ " not in sql


@pytest.mark.unit
def test_path_filter_is_insensitive_by_default() -> None:
    sql, params = _render("file:src")
    assert "files.path ~*" in sql
    assert "src" in _param_values(params)


@pytest.mark.unit
def test_path_filter_flips_when_content_leaf_is_case_sensitive() -> None:
    # KD-1 derive-from-leaf: a content term stamped case:yes flips path to `~`.
    sql, _ = _render("foo case:yes file:src")
    assert "files.path ~ " in sql
    assert "files.path ~*" not in sql


@pytest.mark.unit
def test_lang_filter_is_equality_lowercased() -> None:
    sql, params = _render("lang:GO")
    assert "files.lang =" in sql
    assert "go" in _param_values(params)


@pytest.mark.unit
def test_symbol_filter_is_correlated_exists() -> None:
    sql, params = _render("sym:Handler")
    assert "EXISTS (SELECT symbols.id" in sql
    assert "symbols.file_id = files.id" in sql
    assert "symbols.name ~*" in sql
    assert "Handler" in _param_values(params)


# --------------------------------------------------------------------- boolean shapes


@pytest.mark.unit
def test_and_composition() -> None:
    sql, _ = _render("foo lang:go")
    assert "files.content ILIKE" in sql
    assert " AND " in sql
    assert "files.lang =" in sql


@pytest.mark.unit
def test_or_composition() -> None:
    sql, params = _render("foo OR bar")
    assert " OR " in sql
    assert {"%foo%", "%bar%"} <= _param_values(params)


@pytest.mark.unit
def test_nested_or_within_and() -> None:
    sql, _ = _render("(foo OR bar) lang:go")
    assert " OR " in sql
    assert " AND " in sql
    assert "files.lang =" in sql


# --------------------------------------------------------------------- case threading


@pytest.mark.unit
def test_case_yes_threads_to_content_path_symbol_together() -> None:
    # KD-1: derived global case flips content, path, and symbol operators together.
    sql, _ = _render("case:yes /Handler/ file:x sym:y")
    assert "files.content ~ " in sql
    assert "files.path ~ " in sql
    assert "symbols.name ~ " in sql
    assert "~*" not in sql


@pytest.mark.unit
def test_filter_only_case_yes_defaults_insensitive() -> None:
    # No content/regex leaf to derive from -> path defaults to `~*` (documented divergence).
    sql, _ = _render("case:yes file:x")
    assert "files.path ~*" in sql


@pytest.mark.unit
def test_case_sensitive_override_true_flips_filter_only() -> None:
    # Option D: an explicit override closes the filter-only silent-intent gap.
    sql, _ = _render("case:yes file:x", case_sensitive=True)
    assert "files.path ~ " in sql
    assert "files.path ~*" not in sql


@pytest.mark.unit
def test_case_sensitive_override_false_forces_insensitive() -> None:
    # Regex uses its OWN stamped flag (case:yes -> `~`), NOT the override: the
    # `case_sensitive=` arg only steers filter atoms (path/sym), never content/regex.
    sql, _ = _render("case:yes /foo/", case_sensitive=False)
    assert "files.content ~ " in sql
    assert "files.content ~*" not in sql
    # Prove the override reaches a filter: symbol stays insensitive despite case:yes.
    sql2, _ = _render("case:yes sym:y", case_sensitive=False)
    assert "symbols.name ~*" in sql2


# ------------------------------------------------------------------ resolve_case helper


@pytest.mark.unit
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("case:yes file:x", True),
        ("file:x", False),
        ("a case:no b", False),  # last-wins
        ("case:no case:yes foo", True),
    ],
)
def test_resolve_case_helper(query: str, expected: bool) -> None:
    assert resolve_case(query) is expected


# ---------------------------------------------------------------- limit / projection


@pytest.mark.unit
def test_default_limit_is_200() -> None:
    assert DEFAULT_ROW_LIMIT == 200
    _, params = _render("foo")
    assert 200 in params.values()


@pytest.mark.unit
def test_explicit_limit() -> None:
    stmt = compile_query(parse("foo"), limit=25)
    params = dict(stmt.compile(dialect=postgresql.dialect()).params)
    assert 25 in params.values()


@pytest.mark.unit
def test_negative_limit_raises() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        compile_query(parse("foo"), limit=-1)


@pytest.mark.unit
def test_zero_limit_is_allowed() -> None:
    # 0 is a valid non-negative cap (LIMIT 0 -> empty page); only < 0 is rejected.
    _, params = _render("foo", limit=0)
    assert 0 in params.values()


@pytest.mark.unit
def test_projection_and_ordering() -> None:
    sql, _ = _render("foo")
    assert "SELECT files.id, files.repo_id, files.path, files.lang" in sql
    assert "files.content" not in sql.split("WHERE")[0]  # content only in predicate
    assert "ORDER BY files.repo_id, files.path" in sql


# ------------------------------------------------------------------------- edge atoms


@pytest.mark.unit
def test_leading_dash_is_plain_substring() -> None:
    # `-foo` is a literal Substring in V1 (no negation node).
    _, params = _render("-foo")
    assert "%-foo%" in _param_values(params)
