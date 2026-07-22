"""Unit tests for app.query.semantic_filters: the scanner-level filter/residual split.

Pure, no DB/SDK. Adversarial focus on span-exactness (quoted/regex-valued/adjacent/leading/
trailing atoms), the two worked-example behaviors from the module docstring (absolute-path
prose + the quoting remedy; ``or`` staying prose), the loud per-atom rejection list, and
``QueryParseError`` passthrough (including empty-value atoms, e.g. bare ``repo:``).
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.query.parser import QueryParseError
from app.query.semantic_filters import (
    SemanticFilters,
    UnsupportedSemanticAtomError,
    split_semantic_query,
)

# --------------------------------------------------------------------------- extraction


@pytest.mark.unit
def test_extracts_each_field_kind_with_residual() -> None:
    result = split_semantic_query("repo:acme/widgets file:src/auth.py lang:python auth flow")
    assert result == SemanticFilters(
        repo_patterns=("acme/widgets",),
        path_patterns=("src/auth.py",),
        langs=("python",),
        branches=(),
        residual="auth flow",
    )


@pytest.mark.unit
def test_extracts_branch_atom() -> None:
    result = split_semantic_query("branch:feature/x login flow")
    assert result.branches == ("feature/x",)
    assert result.residual == "login flow"


@pytest.mark.unit
def test_repeated_atoms_of_the_same_field_all_extracted_in_order() -> None:
    result = split_semantic_query("repo:a repo:b hello")
    assert result.repo_patterns == ("a", "b")
    assert result.residual == "hello"


@pytest.mark.unit
def test_no_filters_is_pure_prose() -> None:
    result = split_semantic_query("how do deletion vectors work")
    assert result == SemanticFilters((), (), (), (), "how do deletion vectors work")


# ------------------------------------------------------------------- adversarial span-exactness


@pytest.mark.unit
def test_quoted_value_span_covers_the_quotes() -> None:
    result = split_semantic_query('repo:"acme widgets" hello')
    assert result.repo_patterns == ("acme widgets",)
    assert result.residual == "hello"


@pytest.mark.unit
def test_regex_valued_filter_span_covers_both_slashes() -> None:
    result = split_semantic_query("file:/foo.*/ hello")
    assert result.path_patterns == ("foo.*",)
    assert result.residual == "hello"


@pytest.mark.unit
def test_adjacent_atoms_with_no_interior_prose_yield_empty_residual() -> None:
    result = split_semantic_query("repo:a file:b")
    assert result.repo_patterns == ("a",)
    assert result.path_patterns == ("b",)
    assert result.residual == ""


@pytest.mark.unit
def test_leading_atom() -> None:
    result = split_semantic_query("repo:a hello world")
    assert result.repo_patterns == ("a",)
    assert result.residual == "hello world"


@pytest.mark.unit
def test_trailing_atom() -> None:
    result = split_semantic_query("hello world repo:a")
    assert result.repo_patterns == ("a",)
    assert result.residual == "hello world"


@pytest.mark.unit
def test_interior_prose_spacing_is_preserved_away_from_any_cut() -> None:
    # Only whitespace AT a cut boundary is collapsed; spacing untouched by any excision is
    # preserved byte-exactly.
    result = split_semantic_query("hello    world repo:a")
    assert result.residual == "hello    world"


@pytest.mark.unit
def test_slash_inside_a_bareword_stays_prose() -> None:
    # Only a TOKEN-INITIAL '/' scans as a regex delimiter; a mid-word '/' (e.g. "TCP/IP") is
    # just part of an ordinary bareword/SUBSTRING token and is never excised.
    result = split_semantic_query("what is TCP/IP")
    assert result == SemanticFilters((), (), (), (), "what is TCP/IP")


@pytest.mark.unit
def test_parens_stay_prose() -> None:
    result = split_semantic_query("(auth or login) repo:acme")
    assert result.repo_patterns == ("acme",)
    assert result.residual == "(auth or login)"


# --------------------------------------------------------------- worked example 1: absolute path


@pytest.mark.unit
def test_absolute_path_prose_errors_with_quoting_remedy() -> None:
    # The token-initial '/' in an unquoted absolute path scans as a REGEX token, so this is
    # indistinguishable from a deliberate /regex/ atom -- and is rejected the same way.
    with pytest.raises(UnsupportedSemanticAtomError) as exc_info:
        split_semantic_query("explain /etc/nginx.conf")
    assert exc_info.value.atom == "regex"


@pytest.mark.unit
def test_quoting_the_absolute_path_keeps_it_as_prose() -> None:
    # The remedy: quoting scans the whole thing as ONE SUBSTRING token, never touching the
    # regex path -- the residual is unmangled.
    result = split_semantic_query('explain "/etc/nginx.conf"')
    assert result == SemanticFilters((), (), (), (), 'explain "/etc/nginx.conf"')


# ------------------------------------------------------------- worked example 2: OR is prose


@pytest.mark.unit
def test_or_between_two_repo_atoms_is_prose_not_boolean_structure() -> None:
    # Both repo: atoms extract as filters (AND-composed downstream); the word "or" is not a
    # filter token, so it stays in the residual and embeds as ordinary prose.
    result = split_semantic_query("repo:a or repo:b hello")
    assert result.repo_patterns == ("a", "b")
    assert result.residual == "or hello"


# --------------------------------------------------------------------------- per-atom rejection


@pytest.mark.unit
@pytest.mark.parametrize(
    ("query", "expected_atom"),
    [
        ("sym:Foo bar", "sym:"),
        ("case:yes bar", "case:"),
        ("case:no bar", "case:"),
        ("commit:abc1234 bar", "commit:"),
        ("/foo.*bar/ baz", "regex"),
    ],
)
def test_unsupported_atom_raises_with_position(query: str, expected_atom: str) -> None:
    with pytest.raises(UnsupportedSemanticAtomError) as exc_info:
        split_semantic_query(query)
    assert exc_info.value.atom == expected_atom
    # Every parametrized query puts the offending atom at the very start.
    assert exc_info.value.position == 0


# ------------------------------------------------------------------------- QueryParseError


@pytest.mark.unit
def test_bare_field_value_raises_query_parse_error() -> None:
    with pytest.raises(QueryParseError):
        split_semantic_query("repo:")


@pytest.mark.unit
def test_unterminated_quote_raises_query_parse_error() -> None:
    with pytest.raises(QueryParseError):
        split_semantic_query('repo:"unterminated hello')


@pytest.mark.unit
def test_unterminated_regex_raises_query_parse_error() -> None:
    with pytest.raises(QueryParseError):
        split_semantic_query("/unterminated hello")


@pytest.mark.unit
def test_invalid_commit_value_raises_query_parse_error() -> None:
    # Too short to be a valid git hash prefix (< 7 hex chars): rejected at scan time, before
    # this module's own unsupported-atom check ever runs.
    with pytest.raises(QueryParseError):
        split_semantic_query("commit:ab bar")


# ------------------------------------------------------------------------- empty residual


@pytest.mark.unit
def test_filter_only_query_yields_empty_residual() -> None:
    result = split_semantic_query("repo:acme lang:python")
    assert result.residual == ""


@pytest.mark.unit
def test_empty_query_yields_empty_residual() -> None:
    result = split_semantic_query("")
    assert result.residual == ""


@pytest.mark.unit
def test_whitespace_only_query_yields_empty_residual() -> None:
    result = split_semantic_query("   ")
    assert result.residual == ""


# ----------------------------------------------------------------------- purity guard


@pytest.mark.unit
def test_semantic_filters_import_is_pure() -> None:
    """Importing this module must not drag in db/databricks/psycopg/sqlalchemy/indexer.

    Mirrors ``test_parser_import_is_pure`` (tests/unit/test_query_parser.py): this module
    couples to the parser's private scanning internals, but stays stdlib-only itself, so it
    can be imported and unit-tested without touching the database or the Databricks SDK.
    """
    code = (
        "import app.query.semantic_filters, sys; "
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
