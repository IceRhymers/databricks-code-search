"""Unit tests for the offline resolution-distribution measurement script (AC4).

Pure-Python bucketing/formatting helpers only -- no DB. ``build_candidate_count_select``'s
rendered SQL shape (branch-scoped, edge_kind-filtered, correlated-subquery join) is covered by
``tests/unit/test_references.py``; this file only proves the script's own helpers stay
faithful to the shared ``classify_resolution`` the resolver uses (no independent re-derivation
that could drift).
"""

from __future__ import annotations

import pytest

from scripts.measure_reference_resolution import (
    CALL_BASELINE_PCT,
    ambiguous_histogram,
    bucket_counts,
    format_distribution,
    format_histogram,
)


@pytest.mark.unit
def test_bucket_counts_empty() -> None:
    assert bucket_counts([]) == {"unique": 0, "ambiguous": 0, "unresolved": 0}


@pytest.mark.unit
def test_bucket_counts_classifies_via_shared_helper() -> None:
    # 0 -> unresolved, 1 -> unique, >=2 -> ambiguous (mirrors classify_resolution exactly).
    assert bucket_counts([0, 0, 1, 2, 31]) == {"unique": 1, "ambiguous": 2, "unresolved": 2}


@pytest.mark.unit
def test_ambiguous_histogram_ignores_unique_and_unresolved() -> None:
    assert ambiguous_histogram([0, 1, 2, 2, 3]) == {2: 2, 3: 1}


@pytest.mark.unit
def test_ambiguous_histogram_empty_when_no_ambiguous_sites() -> None:
    assert ambiguous_histogram([0, 0, 1, 1]) == {}


@pytest.mark.unit
def test_format_distribution_includes_counts_and_percentages() -> None:
    text = format_distribution("call edges", {"unique": 1, "ambiguous": 1, "unresolved": 2})
    assert "call edges (n=4):" in text
    assert "unique" in text
    assert "50.0%" in text  # unresolved: 2/4


@pytest.mark.unit
def test_format_distribution_zero_total_does_not_divide_by_zero() -> None:
    text = format_distribution("empty", {"unique": 0, "ambiguous": 0, "unresolved": 0})
    assert "(n=0):" in text
    assert "0.0%" in text


@pytest.mark.unit
def test_format_distribution_with_baseline_renders_comparison() -> None:
    text = format_distribution(
        "call edges", {"unique": 1, "ambiguous": 1, "unresolved": 0}, baseline=CALL_BASELINE_PCT
    )
    assert "baseline 28.8%" in text
    assert "baseline 33.4%" in text
    assert "baseline 37.8%" in text


@pytest.mark.unit
def test_format_histogram_empty() -> None:
    assert "no ambiguous" in format_histogram({})


@pytest.mark.unit
def test_format_histogram_sorted_by_candidate_count() -> None:
    text = format_histogram({3: 1, 2: 5})
    # candidate_count=2 must render before candidate_count=3 (sorted, not insertion order).
    assert text.index("candidate_count=2") < text.index("candidate_count=3")
