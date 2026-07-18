"""Unit tests for the zoekt query parser (issue #8)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.query.parser import (
    And,
    LangFilter,
    Or,
    PathFilter,
    QueryParseError,
    Regex,
    RepoFilter,
    Substring,
    SymbolFilter,
    parse,
)

# --------------------------------------------------------------------------- filters


@pytest.mark.unit
def test_repo_filter() -> None:
    assert parse("repo:x") == RepoFilter("x")


@pytest.mark.unit
def test_file_filter() -> None:
    assert parse("file:src/") == PathFilter("src/")


@pytest.mark.unit
def test_lang_filter() -> None:
    assert parse("lang:go") == LangFilter("go")


@pytest.mark.unit
def test_sym_filter() -> None:
    assert parse("sym:Name") == SymbolFilter("Name")


@pytest.mark.unit
def test_repo_filter_quoted_value() -> None:
    assert parse('repo:"my repo"') == RepoFilter("my repo")


@pytest.mark.unit
def test_file_filter_regex_value() -> None:
    assert parse("file:/foo\\/bar/") == PathFilter("foo/bar")


# ----------------------------------------------------------------------------- atoms


@pytest.mark.unit
def test_bare_substring() -> None:
    assert parse("Foo") == Substring("Foo", False)


@pytest.mark.unit
def test_regex_atom() -> None:
    assert parse("/Foo.*Bar/") == Regex("Foo.*Bar", False)


@pytest.mark.unit
def test_quoted_substring() -> None:
    assert parse('"a b"') == Substring("a b", False)


@pytest.mark.unit
def test_regex_escaped_slash() -> None:
    assert parse("/a\\/b/") == Regex("a/b", False)


@pytest.mark.unit
def test_regex_body_not_compiled() -> None:
    # An invalid Python/POSIX regex body still parses -- bodies are never compiled here.
    assert parse("/[/") == Regex("[", False)


@pytest.mark.unit
def test_quoted_escaped_quote() -> None:
    assert parse('"foo\\"bar"') == Substring('foo"bar', False)


# ------------------------------------------------------------- three-way classification


@pytest.mark.unit
@pytest.mark.parametrize(
    "query",
    [
        "std::vector",
        "http://x",
        "foo::bar",
        "a:b:c",
        "foo:bar",
        "foo:",
        "Repo:x",
        "FILE:y",
        "repo",
    ],
)
def test_non_field_colon_lexemes_are_raw_substrings(query: str) -> None:
    assert parse(query) == Substring(query, False)


@pytest.mark.unit
@pytest.mark.parametrize("query", ["content:x", "branch:main", "r:x", "s:x"])
def test_reserved_fields_raise(query: str) -> None:
    with pytest.raises(QueryParseError):
        parse(query)


@pytest.mark.unit
@pytest.mark.parametrize("query", ["repo:", "case:", "content:"])
def test_empty_field_values_raise(query: str) -> None:
    with pytest.raises(QueryParseError):
        parse(query)


# ------------------------------------------------------------------------------ case


@pytest.mark.unit
def test_case_yes_stamps_substring() -> None:
    assert parse("case:yes Foo") == Substring("Foo", True)


@pytest.mark.unit
def test_case_no_stamps_regex_false() -> None:
    assert parse("case:no /x/") == Regex("x", False)


@pytest.mark.unit
def test_default_case_is_false() -> None:
    assert parse("Foo") == Substring("Foo", False)


@pytest.mark.unit
def test_case_position_independent() -> None:
    assert parse("Foo case:yes") == parse("case:yes Foo") == Substring("Foo", True)


@pytest.mark.unit
def test_case_last_wins() -> None:
    assert parse("case:yes case:no Foo") == Substring("Foo", False)


@pytest.mark.unit
def test_case_as_or_branch_no_dangling() -> None:
    # `case:yes` occupies an operand position (so no dangling error) but adds zero terms.
    assert parse("foo OR case:yes") == Substring("foo", True)


@pytest.mark.unit
def test_case_flag_is_query_global_over_or() -> None:
    # The resolved flag is stamped on every Substring/Regex, including across OR branches.
    assert parse("a OR b case:yes") == Or((Substring("a", True), Substring("b", True)))


@pytest.mark.unit
def test_case_between_or_operators_collapses() -> None:
    # `case:yes` between two ORs is a valid (termless) operand -> no dangling error; and
    # because the global flag is now True it is stamped onto both real terms.
    assert parse("a OR case:yes OR b") == Or((Substring("a", True), Substring("b", True)))


@pytest.mark.unit
def test_case_only_query_raises() -> None:
    with pytest.raises(QueryParseError):
        parse("case:yes")


@pytest.mark.unit
@pytest.mark.parametrize("query", ["case:maybe", "case:auto", "case:YES"])
def test_case_invalid_value_raises(query: str) -> None:
    with pytest.raises(QueryParseError):
        parse(query)


# --------------------------------------------------------------- boolean / precedence


@pytest.mark.unit
def test_juxtaposition_is_and() -> None:
    assert parse("a b") == And((Substring("a"), Substring("b")))


@pytest.mark.unit
def test_lowercase_or() -> None:
    assert parse("a or b") == Or((Substring("a"), Substring("b")))


@pytest.mark.unit
def test_uppercase_or() -> None:
    assert parse("a OR b") == Or((Substring("a"), Substring("b")))


@pytest.mark.unit
def test_mixedcase_or() -> None:
    assert parse("A or B") == Or((Substring("A"), Substring("B")))


@pytest.mark.unit
def test_and_binds_tighter_than_or() -> None:
    assert parse("a b OR c d") == Or(
        (And((Substring("a"), Substring("b"))), And((Substring("c"), Substring("d"))))
    )


@pytest.mark.unit
def test_and_then_single_or_operand() -> None:
    assert parse("a b c OR d") == Or(
        (And((Substring("a"), Substring("b"), Substring("c"))), Substring("d"))
    )


@pytest.mark.unit
def test_parens_override_precedence() -> None:
    result = parse("a (b OR c)")
    expected = And((Substring("a"), Or((Substring("b"), Substring("c")))))
    assert result == expected
    # The Or must NOT be flattened into the And (different operator).
    assert isinstance(result, And)
    assert isinstance(result.children[1], Or)


@pytest.mark.unit
def test_two_or_groups_anded() -> None:
    assert parse("(a OR b) (c OR d)") == And(
        (Or((Substring("a"), Substring("b"))), Or((Substring("c"), Substring("d"))))
    )


@pytest.mark.unit
def test_single_atom_is_bare() -> None:
    assert parse("a") == Substring("a")


@pytest.mark.unit
def test_repeated_atom_no_dedup() -> None:
    result = parse("a a")
    assert result == And((Substring("a"), Substring("a")))
    assert isinstance(result, And)
    assert len(result.children) == 2


@pytest.mark.unit
def test_order_is_preserved() -> None:
    assert parse("a b") != parse("b a")


@pytest.mark.unit
def test_quoted_or_is_literal() -> None:
    assert parse('"or"') == Substring("or")


@pytest.mark.unit
def test_and_is_not_a_keyword() -> None:
    assert parse("a and b") == And((Substring("a"), Substring("and"), Substring("b")))


# ------------------------------------------------------------------ mixed acceptance


@pytest.mark.unit
def test_mixed_filters_and_regex() -> None:
    assert parse("repo:x lang:go /Foo.*Bar/") == And(
        (RepoFilter("x"), LangFilter("go"), Regex("Foo.*Bar", False))
    )


# ---------------------------------------------------------------- dangling / malformed


@pytest.mark.unit
@pytest.mark.parametrize(
    "query",
    ["", "   ", "\t\n", "a OR", "OR a", "a OR OR b", "(a b", "a)", "()"],
)
def test_malformed_queries_raise(query: str) -> None:
    with pytest.raises(QueryParseError):
        parse(query)


@pytest.mark.unit
def test_empty_query_position() -> None:
    with pytest.raises(QueryParseError) as exc:
        parse("")
    assert exc.value.position == 0


@pytest.mark.unit
def test_unterminated_regex_position() -> None:
    with pytest.raises(QueryParseError) as exc:
        parse("/foo")
    assert exc.value.position == 0


@pytest.mark.unit
def test_unterminated_quote_position() -> None:
    with pytest.raises(QueryParseError) as exc:
        parse('"foo')
    assert exc.value.position == 0


@pytest.mark.unit
def test_deep_nesting_raises_parse_error_not_recursion_error() -> None:
    with pytest.raises(QueryParseError) as exc:
        parse("(" * 5000)
    assert "too deep" in str(exc.value)


# ------------------------------------------------------------------------ structural


@pytest.mark.unit
def test_and_or_always_have_at_least_two_children() -> None:
    for query in ["a b", "a OR b", "a b OR c d", "(a OR b) (c OR d)", "a b c OR d"]:
        _assert_arity(parse(query))


def _assert_arity(node: object) -> None:
    if isinstance(node, (And, Or)):
        assert len(node.children) >= 2
        for child in node.children:
            _assert_arity(child)


@pytest.mark.unit
def test_redundant_parens_collapse() -> None:
    assert parse("(a)") == Substring("a")


# ------------------------------------------------------------------ hashability / eq


@pytest.mark.unit
def test_equal_queries_are_equal_and_hashable() -> None:
    a = parse("repo:x lang:go /Foo.*Bar/ OR sym:Name")
    b = parse("repo:x lang:go /Foo.*Bar/ OR sym:Name")
    assert a == b
    assert hash(a) == hash(b)
    assert {a, b} == {a}
    assert {a: 1}[b] == 1


# ----------------------------------------------------------------------- purity guard


@pytest.mark.unit
def test_parser_import_is_pure() -> None:
    """Importing the parser must not drag in db/databricks/psycopg/sqlalchemy/indexer."""
    code = (
        "import app.query.parser, sys; "
        "print('|'.join(m for m in sys.modules "
        "if m.split('.')[0] in {'app', 'databricks', 'psycopg', 'sqlalchemy', 'indexer'}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(filter(None, proc.stdout.strip().split("|")))
    forbidden = {"app.db", "databricks", "psycopg", "sqlalchemy", "indexer"}
    offenders = {m for m in loaded if any(m == f or m.startswith(f + ".") for f in forbidden)}
    assert offenders == set(), f"impure imports: {offenders}"
