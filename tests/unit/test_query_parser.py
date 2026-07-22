"""Unit tests for the zoekt query parser."""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.query.parser import (
    And,
    BranchFilter,
    CommitFilter,
    LangFilter,
    Not,
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
def test_branch_filter() -> None:
    assert parse("branch:main") == BranchFilter("main")


@pytest.mark.unit
def test_commit_filter_prefix() -> None:
    assert parse("commit:abc1234") == CommitFilter("abc1234")


@pytest.mark.unit
def test_commit_filter_full_sha() -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"  # 40 hex chars
    assert parse(f"commit:{sha}") == CommitFilter(sha)


@pytest.mark.unit
def test_commit_filter_case_normalized_to_lowercase() -> None:
    # git object names are lowercase hex; a mixed/upper input is normalized, not rejected.
    assert parse("commit:ABC1234") == CommitFilter("abc1234")


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "xyz1234",  # non-hex letters
        "abc12",  # too short (< 7)
        "0123456789abcdef0123456789abcdef012345678",  # too long (41 > 40)
        "abc123g",  # a single non-hex char anywhere
    ],
)
def test_commit_filter_rejects_invalid_hash(value: str) -> None:
    with pytest.raises(QueryParseError):
        parse(f"commit:{value}")


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
@pytest.mark.parametrize("query", ["content:x", "r:x", "s:x", "b:x"])
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
def test_case_yes_stamps_regex_true() -> None:
    # Guards that the resolved flag reaches Regex construction, not just Substring
    # (case:no above also matches the default False, so it cannot catch a dropped flag).
    assert parse("case:yes /x/") == Regex("x", True)


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
def test_case_only_in_parens_raises() -> None:
    # A group that resolves to only case markers has no real terms -> empty query.
    with pytest.raises(QueryParseError):
        parse("(case:yes)")


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
def test_same_operator_and_group_flattens() -> None:
    # A parenthesized And juxtaposed with another term flattens into one n-ary And.
    assert parse("(a b) c") == And((Substring("a"), Substring("b"), Substring("c")))


@pytest.mark.unit
def test_same_operator_or_group_flattens() -> None:
    # A parenthesized Or OR'd with another term flattens into one n-ary Or.
    assert parse("(a OR b) OR c") == Or((Substring("a"), Substring("b"), Substring("c")))


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
def test_or_order_is_preserved() -> None:
    assert parse("a OR b") != parse("b OR a")


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


# --------------------------------------------------------------------------- negation


@pytest.mark.unit
def test_negation_of_bare_substring() -> None:
    assert parse("-foo") == Not(Substring("foo", False))


@pytest.mark.unit
def test_negation_of_quoted_substring() -> None:
    assert parse('-"a b"') == Not(Substring("a b", False))


@pytest.mark.unit
def test_negation_of_regex() -> None:
    assert parse("-/Foo/") == Not(Regex("Foo", False))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("-repo:acme", Not(RepoFilter("acme"))),
        ("-file:src/", Not(PathFilter("src/"))),
        ("-lang:go", Not(LangFilter("go"))),
        ("-sym:Name", Not(SymbolFilter("Name"))),
        ("-branch:main", Not(BranchFilter("main"))),
        ("-commit:abc1234", Not(CommitFilter("abc1234"))),
    ],
)
def test_negation_of_each_filter_type(query: str, expected: Not) -> None:
    assert parse(query) == expected


@pytest.mark.unit
def test_negation_binds_tighter_than_and() -> None:
    # `-a b` is `(NOT a) AND b`, not `NOT (a AND b)`.
    assert parse("-a b") == And((Not(Substring("a")), Substring("b")))


@pytest.mark.unit
def test_whitespace_and_of_two_negations() -> None:
    assert parse("-a -b") == And((Not(Substring("a")), Not(Substring("b"))))


@pytest.mark.unit
def test_or_with_negation_on_one_arm() -> None:
    assert parse("-a OR b") == Or((Not(Substring("a")), Substring("b")))


@pytest.mark.unit
def test_or_with_negation_on_both_arms() -> None:
    assert parse("-a OR -b") == Or((Not(Substring("a")), Not(Substring("b"))))


@pytest.mark.unit
def test_negation_of_parenthesized_group() -> None:
    assert parse("-(a b)") == Not(And((Substring("a"), Substring("b"))))


@pytest.mark.unit
def test_negation_of_parenthesized_or_group() -> None:
    assert parse("-(a OR b)") == Not(Or((Substring("a"), Substring("b"))))


@pytest.mark.unit
def test_double_negation_is_not_collapsed() -> None:
    # `--foo` stays the exact nested AST Not(Not(...)), never simplified to Substring.
    assert parse("--foo") == Not(Not(Substring("foo")))


@pytest.mark.unit
def test_triple_negation_is_not_collapsed() -> None:
    assert parse("---foo") == Not(Not(Not(Substring("foo"))))


@pytest.mark.unit
def test_negation_case_flag_propagates_to_negated_leaf() -> None:
    # Case is query-global and stamped on the leaf; the Not wrapper is transparent to it.
    assert parse("case:yes -/Foo/") == Not(Regex("Foo", True))
    assert parse("case:yes -foo") == Not(Substring("foo", True))


@pytest.mark.unit
def test_quoted_leading_dash_is_a_literal_substring_not_negation() -> None:
    # The escape hatch: quoting keeps a leading dash as a literal, never a NOT.
    assert parse('"-foo"') == Substring("-foo", False)


# ------------------------------------------------------- negation: compat fallthrough (no NOT)


@pytest.mark.unit
def test_bare_dash_at_eof_is_literal_substring() -> None:
    # `-` with no following char is NOT negation -- it falls through to the bareword scan.
    assert parse("-") == Substring("-")


@pytest.mark.unit
def test_trailing_dash_is_literal_substring() -> None:
    assert parse("foo -") == And((Substring("foo"), Substring("-")))


@pytest.mark.unit
def test_dash_before_whitespace_is_literal_substring() -> None:
    assert parse("a - b") == And((Substring("a"), Substring("-"), Substring("b")))


@pytest.mark.unit
def test_dash_before_rparen_is_literal_substring() -> None:
    assert parse("(a -)") == And((Substring("a"), Substring("-")))


@pytest.mark.unit
def test_interior_dash_is_preserved_in_bareword() -> None:
    assert parse("foo-bar") == Substring("foo-bar")


@pytest.mark.unit
def test_dash_inside_field_value_is_preserved() -> None:
    assert parse("repo:foo-bar") == RepoFilter("foo-bar")


# ------------------------------------------------------------------- negation: depth guard


@pytest.mark.unit
def test_long_flat_chain_of_negations_does_not_trip_depth_guard() -> None:
    # 250 sibling `-a` terms ANDed: each negation is parsed and unwound independently, so depth
    # tracks NESTING (always 1 here), never a running count of NOT tokens across the AND chain.
    query = " ".join(["-a"] * 250)
    result = parse(query)
    assert isinstance(result, And)
    assert len(result.children) == 250
    assert all(child == Not(Substring("a")) for child in result.children)


@pytest.mark.unit
def test_deeply_nested_negation_trips_depth_guard() -> None:
    # `-` * 201 before one operand nests 201 deep (> _MAX_DEPTH == 200) and must raise.
    with pytest.raises(QueryParseError) as exc:
        parse("-" * 201 + "a")
    assert "too deep" in str(exc.value)


@pytest.mark.unit
def test_nesting_just_under_the_guard_is_accepted() -> None:
    # `-` * 200 nests exactly to the limit and parses (200 is not > 200).
    result = parse("-" * 200 + "a")
    node: object = result
    for _ in range(200):
        assert isinstance(node, Not)
        node = node.child
    assert node == Substring("a")


# ---------------------------------------------------------------- negation: error cases


@pytest.mark.unit
def test_negated_or_keyword_raises() -> None:
    # `-or`: NOT applied to a dangling operand -- `or` lexes as the OR keyword, which is not a
    # valid primary, so this raises (matching parse_or's dangling-OR rejection).
    with pytest.raises(QueryParseError):
        parse("-or")


@pytest.mark.unit
def test_negated_uppercase_or_keyword_raises() -> None:
    with pytest.raises(QueryParseError):
        parse("-OR")


@pytest.mark.unit
def test_negated_case_flag_raises() -> None:
    # `case:` is a termless query-global flag; negating it is meaningless -> a loud error at the
    # NOT token's position.
    with pytest.raises(QueryParseError) as exc:
        parse("-case:yes")
    assert exc.value.position == 0


@pytest.mark.unit
def test_double_negated_case_flag_raises() -> None:
    with pytest.raises(QueryParseError):
        parse("--case:yes")


@pytest.mark.unit
def test_negated_case_only_group_raises() -> None:
    with pytest.raises(QueryParseError):
        parse("-(case:yes)")


@pytest.mark.unit
@pytest.mark.parametrize("query", ["-content:x", "-r:x", "-b:x"])
def test_negated_reserved_field_raises(query: str) -> None:
    # The reserved-field rejection fires while scanning the field itself, so wrapping it in NOT
    # changes nothing -- it still raises.
    with pytest.raises(QueryParseError):
        parse(query)


@pytest.mark.unit
def test_negated_empty_group_raises() -> None:
    with pytest.raises(QueryParseError):
        parse("-()")


@pytest.mark.unit
def test_negation_before_rparen_raises_on_the_paren() -> None:
    # `-)`: the '-' is a literal Substring (fallthrough), then a stray ')' -> unexpected ')'.
    with pytest.raises(QueryParseError) as exc:
        parse("-)")
    assert exc.value.position == 1


@pytest.mark.unit
def test_negation_then_unterminated_group_raises() -> None:
    with pytest.raises(QueryParseError):
        parse("-(")


@pytest.mark.unit
def test_negated_query_is_hashable_and_equal() -> None:
    a = parse("-foo -(bar OR baz)")
    b = parse("-foo -(bar OR baz)")
    assert a == b
    assert hash(a) == hash(b)


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
