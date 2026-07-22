"""Unit tests for grep line extraction + matcher building (issue #10).

Pure, no DB. Targets :func:`extract_line_matches` and :func:`_build_matchers`; byte
offsets are asserted against the invariant ``line_text.encode("utf-8")[s:e] == matched``.
"""

from __future__ import annotations

import ast
import inspect
import re

import pytest

from app.query.parser import parse, resolve_case
from app.search import grep as grep_module
from app.search.grep import (
    LineMatch,
    _build_matchers,
    _no_content_atom,
    _zero_width_only_atoms,
    extract_line_matches,
)


def _patterns(query: str) -> tuple[list[re.Pattern[str]], bool]:
    """Build matchers the way ``grep_search`` does: parse + resolve_case + _build_matchers."""
    return _build_matchers(parse(query), resolve_case(query))


def _matches(query: str, content: str) -> list[LineMatch]:
    patterns, _ = _patterns(query)
    return extract_line_matches(content, patterns)


# --------------------------------------------------------------------------- substring


@pytest.mark.unit
def test_substring_single_match_line_number_text_and_byte_range() -> None:
    matches = _matches("foo", "first line\nsecond foo here")
    assert len(matches) == 1
    (m,) = matches
    assert m.line_number == 2
    assert m.line_text == "second foo here"
    assert m.byte_ranges == ((7, 10),)
    assert m.line_text.encode("utf-8")[7:10] == b"foo"


@pytest.mark.unit
def test_substring_case_insensitive_matches_any_letter_case() -> None:
    matches = _matches("Foo", "foo\nFOO")
    assert [m.line_number for m in matches] == [1, 2]


@pytest.mark.unit
def test_substring_case_sensitive_matches_only_exact_case() -> None:
    matches = _matches("case:yes Foo", "foo\nFOO\nFoo")
    assert [m.line_number for m in matches] == [3]


# ----------------------------------------------------------------------------- regex


@pytest.mark.unit
def test_regex_case_insensitive_and_case_sensitive() -> None:
    insensitive = _matches("/F.o/", "foo\nFoo\nbar")
    assert [m.line_number for m in insensitive] == [1, 2]

    sensitive = _matches("case:yes /F.o/", "foo\nFoo\nbar")
    assert [m.line_number for m in sensitive] == [2]


# --------------------------------------------------------------- multiple / overlapping


@pytest.mark.unit
def test_multiple_matches_on_one_line_are_sorted_non_overlapping() -> None:
    matches = _matches("foo", "foo and foo")
    (m,) = matches
    assert m.byte_ranges == ((0, 3), (8, 11))


@pytest.mark.unit
def test_overlapping_spans_from_two_atoms_are_merged() -> None:
    # "foobar" -> [0,6); "bar" -> [3,6); overlap merges into a single [0,6).
    matches = _matches("foobar OR bar", "foobar")
    (m,) = matches
    assert m.byte_ranges == ((0, 6),)


# -------------------------------------------------------------------------------- utf-8


@pytest.mark.unit
def test_utf8_byte_range_differs_from_char_index() -> None:
    # "你好" = 2 chars / 6 bytes, then a space, then "foo": char span [3,6) but bytes [7,10).
    matches = _matches("foo", "你好 foo")
    (m,) = matches
    assert m.byte_ranges == ((7, 10),)
    s, e = m.byte_ranges[0]
    assert m.line_text.encode("utf-8")[s:e] == b"foo"


@pytest.mark.unit
def test_utf8_multiple_spans_with_multibyte_gap_between_them() -> None:
    # Two "foo" matches separated by a multibyte run: the incremental gap-encoding must
    # keep every span's byte offsets exact, not just the first.
    line = "foo 你好 foo"
    matches = _matches("foo", line)
    (m,) = matches
    assert len(m.byte_ranges) == 2
    for start, end in m.byte_ranges:
        assert line.encode("utf-8")[start:end] == b"foo"
    # Second span starts past the 6-byte "你好" run, not at its char index.
    assert m.byte_ranges == ((0, 3), (11, 14))


# --------------------------------------------------------------------------- empty sets


@pytest.mark.unit
def test_no_match_content_yields_no_line_matches() -> None:
    assert _matches("foo", "nothing here\nstill nothing") == []


@pytest.mark.unit
def test_empty_content_yields_no_line_matches() -> None:
    assert _matches("foo", "") == []


# ---------------------------------------------------------------------------- crlf / zero-width


@pytest.mark.unit
def test_crlf_trailing_carriage_return_is_stripped() -> None:
    matches = _matches("foo", "alpha foo\r\nbeta")
    (m,) = matches
    assert m.line_text == "alpha foo"
    assert m.byte_ranges == ((6, 9),)
    assert m.line_text.encode("utf-8")[6:9] == b"foo"


@pytest.mark.unit
def test_zero_width_regex_matches_are_dropped() -> None:
    patterns = [re.compile("a*"), re.compile("^")]
    matches = extract_line_matches("aaa", patterns)
    (m,) = matches
    # Only the real [0,3) span survives; the trailing "a*" and "^" zero-width hits drop.
    assert m.byte_ranges == ((0, 3),)
    assert all(end > start for start, end in m.byte_ranges)


# -------------------------------------------------------------------------- NOT-RE2 / nesting


@pytest.mark.unit
def test_uncompilable_regex_flags_incompatible_without_crashing() -> None:
    # "/*/" is a valid parser Regex but "*" is "nothing to repeat" in Python re.
    patterns, regex_incompatible = _patterns("/*/ foo")
    assert regex_incompatible is True
    assert len(patterns) == 1  # the substring survives
    matches = extract_line_matches("foo here", patterns)
    assert [m.line_number for m in matches] == [1]


@pytest.mark.unit
def test_negated_content_atom_contributes_no_highlight_pattern() -> None:
    # grep only highlights POSITIVE matches; a `-foo` exclusion is SQL-only and is never
    # rendered as a highlight, so it collects no pattern (the positive `bar` survives).
    patterns, regex_incompatible = _patterns("-foo bar")
    assert regex_incompatible is False
    assert [p.pattern for p in patterns] == ["bar"]
    # A purely-negated query collects nothing at all -> filter-only at the grep layer.
    only_neg, _ = _patterns("-foo")
    assert only_neg == []


@pytest.mark.unit
def test_negated_broken_regex_does_not_flag_regex_incompatible() -> None:
    # JUDGMENT CALL (plan: "reason it through"): a purely-negated broken regex (`-/[/`) builds
    # NO highlight pattern, so nothing was meant to highlight and there is nothing whose
    # highlighting capability was lost. We deliberately do NOT recurse into the Not to attempt a
    # Python re.compile, so regex_incompatible stays False. (The SQL predicate still compiles the
    # pattern server-side via Postgres POSIX ARE regardless of polarity, so exclusion
    # correctness is unaffected -- only the Python-side highlight signal is suppressed.)
    patterns, regex_incompatible = _patterns("-/[/")
    assert patterns == []
    assert regex_incompatible is False
    # Contrast: the SAME broken regex un-negated DOES flag incompatible (it was meant to
    # highlight and Python could not compile it).
    _, positive_incompatible = _patterns("/[/")
    assert positive_incompatible is True


@pytest.mark.unit
def test_matchers_collect_across_and_or_nesting_and_filters_contribute_none() -> None:
    patterns, regex_incompatible = _patterns("(foo OR bar) baz")
    assert regex_incompatible is False
    assert len(patterns) == 3

    filter_only, _ = _patterns("file:src")
    assert filter_only == []
    assert extract_line_matches("anything with foo", filter_only) == []


# ---------------------------------------------------------------------------- multi-line


@pytest.mark.unit
def test_multiline_line_numbers_including_final_unterminated_line() -> None:
    matches = _matches("foo", "foo\nbar foo\nno match\nfoo")
    assert [m.line_number for m in matches] == [1, 2, 4]


# ------------------------------------------------------------------ query-shape flags (#31)
#
# Both helpers are driven through the real parse + _build_matchers pipeline (via `_patterns`)
# rather than hand-built pattern lists, so what is pinned is the flag a real query produces.


def _flags(query: str) -> tuple[bool, bool]:
    """Compute (no_content_atom, zero_width_only_atoms) exactly as ``grep_search`` does."""
    patterns, regex_incompatible = _patterns(query)
    return (
        _no_content_atom(patterns, regex_incompatible),
        _zero_width_only_atoms(patterns, regex_incompatible),
    )


@pytest.mark.unit
@pytest.mark.parametrize("query", ["lang:go", "repo:acme file:.md"])
def test_no_content_atom_true_for_filter_only_query(query: str) -> None:
    no_content, zero_width = _flags(query)
    assert no_content is True
    assert zero_width is False


@pytest.mark.unit
def test_no_content_atom_false_for_uncompilable_regex() -> None:
    # "/[/" is a valid parser Regex that Python re rejects, so _collect_matchers appends
    # nothing -> patterns == [] just like a filter-only query. It is NOT filter-only: it has a
    # content atom, and regex_incompatible is already its signal. Pins the conjunction itself,
    # not just its inputs.
    patterns, regex_incompatible = _patterns("/[/")
    assert patterns == []
    assert regex_incompatible is True
    assert _no_content_atom(patterns, regex_incompatible) is False


@pytest.mark.unit
def test_sym_only_query_is_structurally_filter_only_at_grep_layer() -> None:
    # grep reports the RAW structural fact and does not special-case SymbolFilter: a direct
    # caller running no symbol leg genuinely gets nothing here. The envelope suppresses it.
    no_content, _ = _flags("sym:Handler")
    assert no_content is True


@pytest.mark.unit
@pytest.mark.parametrize("query", [r"/^/", r"/\b/", r"/^/ OR /\b/"])
def test_zero_width_only_atoms_true_for_caret_and_word_boundary(query: str) -> None:
    patterns, regex_incompatible = _patterns(query)
    assert regex_incompatible is False
    assert _zero_width_only_atoms(patterns, regex_incompatible) is True
    assert _no_content_atom(patterns, regex_incompatible) is False


@pytest.mark.unit
def test_zero_width_only_atoms_false_when_regex_incompatible() -> None:
    # "/^/ /[/": the "[" atom never compiled, so its highlighting capability is UNKNOWN, not
    # proven zero-width. Without the regex_incompatible conjunct this returns True -- and the
    # soundness claim that justifies depending on a private CPython API is false.
    patterns, regex_incompatible = _patterns("/^/ /[/")
    assert [p.pattern for p in patterns] == ["^"]
    assert regex_incompatible is True
    assert _zero_width_only_atoms(patterns, regex_incompatible) is False


@pytest.mark.unit
@pytest.mark.parametrize("query", ["foo", "foo OR /^/"])
def test_zero_width_only_atoms_false_for_normal_and_mixed_atoms(query: str) -> None:
    # ANY non-zero-width atom disqualifies: that atom can still produce a highlight.
    _, zero_width = _flags(query)
    assert zero_width is False


@pytest.mark.unit
def test_zero_width_only_atoms_false_for_empty_patterns() -> None:
    # Pins the bool(patterns) conjunct: "every atom is zero-width" is vacuously true over an
    # empty set, but that case is no_content_atom's, and the two must stay disjoint.
    assert _zero_width_only_atoms([], False) is False


@pytest.mark.unit
def test_zero_width_only_atoms_known_incompleteness_documents_a_star() -> None:
    # CORRECT NEGATIVE, not a miss: "a*" can genuinely produce a non-empty span --
    # re.compile("a*").finditer("bar") yields (1, 2), and test_zero_width_regex_matches_are_dropped
    # pins exactly that -- so flagging it would be UNSOUND. getwidth()'s huge max_width is the
    # right answer here, not a limitation to route around.
    #
    # The real residual gap is corpus-dependent and not statically decidable: "a*" over a corpus
    # containing no "a" returns zero files unflagged. Do NOT try to close that by relaxing
    # getwidth()[1] (max) toward getwidth()[0] (min) -- that would flag "a*" and break soundness,
    # which is the whole basis for accepting the private-API dependency.
    _, zero_width = _flags("/a*/")
    assert zero_width is False


@pytest.mark.unit
@pytest.mark.parametrize("query", ['""', "//"])
def test_zero_width_only_atoms_true_for_empty_terms(query: str) -> None:
    # Empty terms are REACHABLE through both routes -- parse('""') -> Substring(value='') and
    # parse('//') -> Regex(pattern='') -- and True is CORRECT for them: re.compile("") has
    # width (0, 0) and its finditer yields only zero-width hits, all dropped by
    # extract_line_matches. Guards against a "fix" based on a false unreachability belief.
    patterns, regex_incompatible = _patterns(query)
    assert len(patterns) == 1
    assert _zero_width_only_atoms(patterns, regex_incompatible) is True


@pytest.mark.unit
def test_getwidth_private_api_canary() -> None:
    # DELIBERATELY UNGUARDED (no try/except). _zero_width_only_atoms depends on the private
    # re._parser, whose failure mode is a silent degrade to False. This canary is the entire
    # justification for accepting that dependency: it must fail CI loudly on CPython drift
    # rather than let the feature rot quietly. Do not wrap it.
    assert re._parser.parse("^").getwidth()[1] == 0  # type: ignore[attr-defined]
    assert re._parser.parse("foo").getwidth()[1] == 3  # type: ignore[attr-defined]


@pytest.mark.unit
def test_zero_width_helper_returns_false_when_parser_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Containment: a future CPython that moves/changes re._parser degrades the flag to False
    # (the pre-#31 status quo), never propagates into grep_search.
    class _Broken:
        def parse(self, _src: str) -> object:
            raise AttributeError("re._parser moved")

    monkeypatch.setattr(re, "_parser", _Broken(), raising=False)
    assert _zero_width_only_atoms([re.compile("^")], False) is False


@pytest.mark.unit
def test_grep_module_does_not_import_private_re_parser_at_module_scope() -> None:
    # The access site is load-bearing. A module-scope `import re._parser` (or a module-scope
    # getattr(re, "_parser")) that fails on a future CPython takes down app/search/grep.py,
    # hence app/main.py, hence the whole MCP server -- corruption instead of containment, which
    # voids the entire basis for using the private API. Assert no TOP-LEVEL statement mentions
    # the name at all, which catches both forms.
    #
    # Note the scope is deliberately broad: module-level string CONSTANTS are included (that is
    # what catches getattr(re, "_parser")), so the module docstring is in scope too. Writing the
    # literal `re._parser` into it will fail this test. That is an accepted false positive --
    # narrowing it would reopen the getattr hole. Refer to the parser obliquely in prose instead.
    tree = ast.parse(inspect.getsource(grep_module))
    referenced: set[str] = set()
    for stmt in tree.body:
        # Skip def/class bodies wholesale: guarded access inside a function is the REQUIRED
        # form, so only statements that execute at import time are in scope here.
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Import):
                referenced |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                referenced |= {node.module or ""} | {alias.name for alias in node.names}
            elif isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                referenced.add(node.value)  # catches getattr(re, "_parser")

    offenders = sorted(name for name in referenced if "_parser" in name)
    assert not offenders, f"module-scope reference to a private re parser: {offenders}"


@pytest.mark.unit
@pytest.mark.parametrize("query", ["lang:go", "/^/", "foo", "sym:X", '""'])
def test_flags_are_mutually_exclusive(query: str) -> None:
    # Three states, never four: no_content_atom needs `not patterns`, zero_width_only_atoms
    # needs `bool(patterns)`. The envelope only ever clears flags, so this holds there too.
    no_content, zero_width = _flags(query)
    assert not (no_content and zero_width)
