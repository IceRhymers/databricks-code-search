"""Unit tests for grep line extraction + matcher building (issue #10).

Pure, no DB. Targets :func:`extract_line_matches` and :func:`_build_matchers`; byte
offsets are asserted against the invariant ``line_text.encode("utf-8")[s:e] == matched``.
"""

from __future__ import annotations

import re

import pytest

from app.query.parser import parse, resolve_case
from app.search.grep import LineMatch, _build_matchers, extract_line_matches


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
