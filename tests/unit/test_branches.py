"""Unit tests for indexer.branches.resolve_branches: glob match, dedup, cap, completeness."""

from __future__ import annotations

import logging

import pytest

from indexer.branches import SOFT_BRANCH_CAP, resolve_branches


@pytest.mark.unit
def test_empty_globs_is_default_branch_only() -> None:
    """The documented default: no branches: configured -> just the default."""
    got = resolve_branches("main", ["main", "dev", "release/1.0"], [], repo="acme/widgets")
    assert got.branches == ["main"]


@pytest.mark.unit
def test_empty_globs_ignores_all_branches_entirely() -> None:
    """Even a nonempty all_branches list is irrelevant when globs is empty."""
    got = resolve_branches("main", [], [], repo="acme/widgets")
    assert got.branches == ["main"]


@pytest.mark.unit
def test_empty_globs_is_always_complete() -> None:
    """AC1/AC3: the default-branch-only fast path can never be truncated."""
    got = resolve_branches("main", ["main", "dev"], [], repo="acme/widgets")
    assert got.complete is True
    assert got.dropped == ()


@pytest.mark.unit
def test_default_branch_always_included_even_if_unmatched() -> None:
    got = resolve_branches("main", ["main", "dev"], ["release/*"], repo="acme/widgets")
    assert "main" in got.branches


@pytest.mark.unit
def test_no_glob_match_still_resolves_to_just_the_default_and_is_complete() -> None:
    """A glob that matches nothing still yields a complete resolution of [default]."""
    got = resolve_branches("main", ["main", "dev"], ["release/*"], repo="acme/widgets")
    assert got.branches == ["main"]
    assert got.complete is True
    assert got.dropped == ()


@pytest.mark.unit
def test_glob_matches_via_fnmatchcase() -> None:
    got = resolve_branches(
        "main",
        ["main", "release/1.0", "release/2.0", "dev"],
        ["release/*"],
        repo="acme/widgets",
    )
    assert set(got.branches) == {"main", "release/1.0", "release/2.0"}


@pytest.mark.unit
def test_fnmatchcase_is_case_sensitive() -> None:
    got = resolve_branches("main", ["Release/1.0"], ["release/*"], repo="acme/widgets")
    assert got.branches == ["main"]  # "Release/1.0" does not match the lowercase glob


@pytest.mark.unit
def test_multiple_globs_are_unioned() -> None:
    got = resolve_branches(
        "main",
        ["main", "staging", "hotfix/x", "dev"],
        ["staging", "hotfix/*"],
        repo="acme/widgets",
    )
    assert set(got.branches) == {"main", "staging", "hotfix/x"}


@pytest.mark.unit
def test_default_branch_matching_a_glob_is_not_duplicated() -> None:
    got = resolve_branches("main", ["main", "dev"], ["m*"], repo="acme/widgets")
    assert got.branches == ["main"]


@pytest.mark.unit
def test_ordering_is_default_first_then_alphabetical() -> None:
    got = resolve_branches("main", ["main", "zeta", "alpha", "beta"], ["*"], repo="acme/widgets")
    assert got.branches == ["main", "alpha", "beta", "zeta"]


@pytest.mark.unit
def test_complete_selection_under_the_cap_is_complete() -> None:
    """A normal, un-truncated resolution reports complete=True with no dropped names."""
    got = resolve_branches(
        "main", ["main", "staging", "dev"], ["*"], repo="acme/widgets", cap=SOFT_BRANCH_CAP
    )
    assert got.complete is True
    assert got.dropped == ()
    assert got.cap == SOFT_BRANCH_CAP


@pytest.mark.unit
def test_cap_truncates_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    all_branches = ["main"] + [f"b{i:02d}" for i in range(30)]
    with caplog.at_level(logging.WARNING, logger="indexer.branches"):
        got = resolve_branches("main", all_branches, ["*"], repo="acme/widgets", cap=5)

    assert len(got.branches) == 5
    assert got.branches[0] == "main"
    # default-first then alphabetical, truncated at 5: main, b00, b01, b02, b03.
    assert got.branches == ["main", "b00", "b01", "b02", "b03"]
    assert got.complete is False
    assert got.cap == 5

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "acme/widgets" in message
    assert "31 branches" in message
    assert "dropping 26" in message


@pytest.mark.unit
def test_cap_overflow_dropped_names_exact(caplog: pytest.LogCaptureFixture) -> None:
    """AC2: the exact set of dropped branches is reported, not just a count."""
    all_branches = ["main"] + [f"b{i:02d}" for i in range(30)]
    with caplog.at_level(logging.WARNING, logger="indexer.branches"):
        got = resolve_branches("main", all_branches, ["*"], repo="acme/widgets", cap=5)

    # 31 total (main + b00..b29), kept 5 (main, b00-b03), dropped the rest (b04-b29).
    expected_dropped = tuple(f"b{i:02d}" for i in range(4, 30))
    assert got.dropped == expected_dropped
    assert set(got.branches) & set(got.dropped) == set()

    message = caplog.records[0].getMessage()
    for name in expected_dropped:
        assert name in message


@pytest.mark.unit
def test_warning_text_states_discovery_incomplete_and_reconciliation_blocked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D2: the truncation warning must name the reconciliation consequence, not just the count."""
    all_branches = ["main"] + [f"b{i:02d}" for i in range(30)]
    with caplog.at_level(logging.WARNING, logger="indexer.branches"):
        resolve_branches("main", all_branches, ["*"], repo="acme/widgets", cap=5)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1  # single log site, no double-log
    message = warnings[0].getMessage()
    assert "incomplete" in message.lower()
    assert "reconciliation" in message.lower()
    assert "blocked" in message.lower()


@pytest.mark.unit
def test_no_warning_when_under_the_cap(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="indexer.branches"):
        resolve_branches("main", ["main", "dev"], ["*"], repo="acme/widgets", cap=5)
    assert caplog.records == []


@pytest.mark.unit
def test_exactly_at_cap_is_complete_with_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The boundary: exactly ``cap`` branches must NOT be treated as truncated.

    A false ``complete=False`` here would needlessly block reconciliation for a
    repo whose discovery was actually complete.
    """
    all_branches = ["main"] + [f"b{i:02d}" for i in range(4)]  # main + 4 = 5 total, cap=5
    with caplog.at_level(logging.WARNING, logger="indexer.branches"):
        got = resolve_branches("main", all_branches, ["*"], repo="acme/widgets", cap=5)

    assert len(got.branches) == 5
    assert got.complete is True
    assert got.dropped == ()
    assert caplog.records == []


@pytest.mark.unit
def test_default_cap_is_the_module_constant() -> None:
    all_branches = [f"b{i:02d}" for i in range(SOFT_BRANCH_CAP + 5)]
    got = resolve_branches("main", all_branches, ["*"], repo="acme/widgets")
    assert len(got.branches) == SOFT_BRANCH_CAP
    assert got.cap == SOFT_BRANCH_CAP


@pytest.mark.unit
def test_default_branch_flip_is_complete_and_drops_the_old_default() -> None:
    """AC3: a default-branch flip (main -> master retired) is valid retirement
    evidence ONLY because discovery is complete -- distinct from the cap-overflow
    case, where an omitted branch must NOT be treated as retired (AC2).
    """
    got = resolve_branches("main", ["main", "master"], ["main"], repo="acme/widgets")
    assert got.branches == ["main"]
    assert got.complete is True
    assert got.dropped == ()
