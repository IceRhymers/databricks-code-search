"""Unit tests for the query compiler: AST -> SQLAlchemy select.

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


# ------------------------------------------------------------------------- branch (0003)


@pytest.mark.unit
def test_branch_filter_is_gin_served_array_contains() -> None:
    sql, params = _render("branch:main")
    assert "files.branches @>" in sql
    assert ["main"] in params.values()
    # An explicit branch: opts out of the implicit default-branch conjunct.
    assert "EXISTS (SELECT repos.id" not in sql


@pytest.mark.unit
def test_no_branch_filter_ands_in_implicit_default_conjunct() -> None:
    sql, _params = _render("foo")
    assert "EXISTS (SELECT repos.id" in sql
    assert "repos.id = files.repo_id" in sql
    assert "coalesce(repos.default_branch," in sql
    assert "= ANY (files.branches)" in sql


@pytest.mark.unit
def test_default_conjunct_coalesce_defaults_to_head() -> None:
    sql, params = _render("foo")
    assert "HEAD" in _param_values(params)


@pytest.mark.unit
def test_branch_filter_nested_in_and_still_suppresses_default_conjunct() -> None:
    sql, _params = _render("foo branch:main")
    assert "files.branches @>" in sql
    assert "EXISTS (SELECT repos.id" not in sql


@pytest.mark.unit
def test_branch_filter_nested_in_or_still_suppresses_default_conjunct() -> None:
    sql, _params = _render("foo OR branch:main")
    assert "files.branches @>" in sql
    assert "EXISTS (SELECT repos.id" not in sql


# ------------------------------------------------------------------ commit (git-hash search)


@pytest.mark.unit
def test_commit_filter_lowers_to_repo_branches_exists() -> None:
    sql, params = _render("commit:abc1234")
    # An EXISTS against repo_branches joined on repo_id (NOT a repo-name regex over-match),
    # scoped to files carrying that branch, with a lowered prefix LIKE.
    assert "EXISTS (SELECT repo_branches.id" in sql
    assert "repo_branches.repo_id = files.repo_id" in sql
    assert "files.branches @> ARRAY[repo_branches.branch]" in sql
    assert "lower(repo_branches.last_indexed_commit) LIKE" in sql
    # The prefix binds as an auto-named param (excluded by _param_values), so assert on raw values.
    assert "abc1234" in set(params.values())


@pytest.mark.unit
def test_commit_filter_reads_repo_branches_never_files_commit() -> None:
    # The one truth-source is repo_branches.last_indexed_commit; files.commit is never read.
    sql, _ = _render("commit:abc1234")
    assert "files.commit" not in sql


@pytest.mark.unit
def test_commit_filter_suppresses_implicit_default_branch_conjunct() -> None:
    # CRITICAL (consensus iter 2): a commit scope opts out of the implicit default-branch
    # conjunct -- the repo_branches EXISTS is present while the repos default EXISTS is ABSENT,
    # else a commit resolving to a non-default branch silently intersects to zero rows.
    sql, _ = _render("commit:abc1234")
    assert "EXISTS (SELECT repo_branches.id" in sql
    assert "EXISTS (SELECT repos.id" not in sql


@pytest.mark.unit
def test_commit_filter_nested_in_and_still_suppresses_default_conjunct() -> None:
    sql, _ = _render("foo commit:abc1234")
    assert "EXISTS (SELECT repo_branches.id" in sql
    assert "EXISTS (SELECT repos.id" not in sql


@pytest.mark.unit
def test_two_commit_atoms_use_distinct_bind_params() -> None:
    # `commit:a OR commit:b` must NOT collide on one :prefix param (consensus iter 3): each
    # CommitFilter lowers to its OWN EXISTS with its OWN auto-named bind.
    sql, params = _render("commit:aaaaaaa OR commit:bbbbbbb")
    assert sql.count("EXISTS (SELECT repo_branches.id") == 2
    values = set(params.values())
    assert "aaaaaaa" in values
    assert "bbbbbbb" in values


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
    assert "ORDER BY files.repo_id, files.path, files.content_sha" in sql


# --------------------------------------------------------------------------- negation


@pytest.mark.unit
def test_leading_dash_lowers_to_negation() -> None:
    # Replaces the retired test_leading_dash_is_plain_substring: `-foo` is now negation. SQLAlchemy
    # renders not_() of an ILIKE by pushing the NOT into the operator (`NOT ILIKE`), still binding
    # the %foo% literal -- a plain negation, not a literal `%-foo%` substring.
    sql, params = _render("-foo")
    assert "files.content NOT ILIKE" in sql
    assert "%foo%" in _param_values(params)
    assert "%-foo%" not in _param_values(params)  # NOT the retired literal-substring behavior


@pytest.mark.unit
def test_negated_regex_wraps_the_regex_operator_in_not() -> None:
    sql, _ = _render("-/foo/")
    assert "NOT (files.content ~*" in sql


@pytest.mark.unit
def test_negation_null_semantics_is_plain_not_no_is_null_rewrite() -> None:
    # Three-valued boolean NOT only: the predicate is a plain negation with NO set-complement
    # `IS NULL OR NOT ...` clause. A NULL content row matches neither the positive nor the
    # negated form -- standard SQL semantics.
    sql, _ = _render("-foo")
    assert "files.content NOT ILIKE" in sql
    assert "IS NULL" not in sql


@pytest.mark.unit
def test_repeated_negations_bind_distinct_params() -> None:
    # `-foo -foo` must bind TWO distinct params (never interpolate, never collide on one name).
    sql, params = _render("-foo -foo")
    assert sql.count("files.content NOT ILIKE") == 2
    like_params = [v for v in params.values() if v == "%foo%"]
    assert len(like_params) == 2  # two separate binds, both %foo%


@pytest.mark.unit
def test_nested_negated_groups_bind_distinct_params() -> None:
    sql, params = _render("-(foo) -(foo)")
    assert sql.count("files.content NOT ILIKE") == 2
    assert len([v for v in params.values() if v == "%foo%"]) == 2


@pytest.mark.unit
def test_negated_content_still_gets_default_branch_conjunct() -> None:
    # A negated content atom carries no branch scope, so the implicit default-branch conjunct is
    # still ANDed in (a `-foo` search runs the default-branch-scoped candidate scan).
    sql, _ = _render("-foo")
    assert "EXISTS (SELECT repos.id" in sql


# ------------------------------------------------- negation: default-branch polarity matrix


@pytest.mark.unit
def test_negated_branch_does_not_opt_out_of_default_conjunct() -> None:
    # `-branch:x foo`: the `-branch:x` is an EXCLUSION, not a selection, so it does NOT opt out
    # of the implicit default-branch conjunct -- both the negated @> AND the default EXISTS emit.
    sql, _ = _render("-branch:main foo")
    assert "NOT (files.branches @>" in sql
    assert "EXISTS (SELECT repos.id" in sql


@pytest.mark.unit
def test_affirmative_branch_beside_negated_branch_opts_out() -> None:
    # `branch:main -branch:x`: the affirmative branch:main opts out (existing behavior), so the
    # default-branch EXISTS is absent even though a negated branch is also present.
    sql, _ = _render("branch:main -branch:dev foo")
    assert "files.branches @>" in sql
    assert "NOT (files.branches @>" in sql
    assert "EXISTS (SELECT repos.id" not in sql


@pytest.mark.unit
def test_double_negated_branch_opts_out_again() -> None:
    # `-(-branch:x)`: double negation -> affirmative again -> opts out of the default conjunct.
    sql, _ = _render("-(-branch:main) foo")
    assert "NOT (NOT (files.branches @>" in sql
    assert "EXISTS (SELECT repos.id" not in sql


@pytest.mark.unit
def test_plain_branch_filter_still_opts_out_unchanged() -> None:
    # Regression: a plain affirmative branch: is unchanged by the polarity rewrite.
    sql, _ = _render("branch:main foo")
    assert "files.branches @>" in sql
    assert "EXISTS (SELECT repos.id" not in sql


@pytest.mark.unit
def test_negated_commit_does_not_opt_out_of_default_conjunct() -> None:
    # A negated commit scope is likewise an exclusion, not a selection -> default conjunct kept.
    sql, _ = _render("-commit:abc1234 foo")
    assert "NOT (EXISTS (SELECT repo_branches.id" in sql
    assert "EXISTS (SELECT repos.id" in sql


@pytest.mark.unit
def test_negated_filters_thread_global_case_through_the_wrapper() -> None:
    # `case:yes -sym:y`: the derived global case flag reaches the negated symbol filter (`~`).
    sql, _ = _render("case:yes foo -sym:y")
    assert "symbols.name ~ " in sql
    assert "symbols.name ~*" not in sql
