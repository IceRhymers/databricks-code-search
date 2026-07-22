"""Unit tests for indexer.store.reconcile_retired_branches's pre-transaction guards.

Mirrors the #14 Phase 2 unit/integration split: these tests prove the sanitizer
and no-op paths never open a transaction or touch the connection at all -- the
real DML (repo lock, membership strip, cascade delete, registry cleanup) is
covered against a live Postgres in tests/integration/test_reconcile.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from indexer.store import ReconcileCounts, reconcile_retired_branches


class _PoisonConnection:
    """Stands in for a Connection that must never be used.

    Any attribute access (``.begin``, ``.execute``, ...) raises, so a test
    using this fixture fails loudly if the no-op path falls through into SQL.
    """

    def __getattr__(self, item: str) -> Any:
        raise AssertionError(f"connection.{item} must not be touched on the no-op path")


@pytest.mark.unit
def test_empty_retired_branches_is_a_noop() -> None:
    counts = reconcile_retired_branches(
        _PoisonConnection(), name="acme/widgets", retired_branches=[]
    )  # type: ignore[arg-type]
    assert counts == ReconcileCounts(branches_removed=0, files_stripped=0, files_deleted=0)


@pytest.mark.unit
def test_all_none_or_blank_entries_is_a_noop() -> None:
    counts = reconcile_retired_branches(
        _PoisonConnection(),  # type: ignore[arg-type]
        name="acme/widgets",
        retired_branches=[None, "", None],  # type: ignore[list-item]
    )
    assert counts == ReconcileCounts(branches_removed=0, files_stripped=0, files_deleted=0)


@pytest.mark.unit
def test_mixed_valid_and_poison_entries_does_not_short_circuit() -> None:
    """A genuinely retired branch alongside None/blank entries must NOT no-op.

    This is the sanitizer's core guarantee: filtering poison out must not also
    filter out legitimate work -- only an ALL-invalid input is a no-op.
    """
    with pytest.raises(AssertionError, match="must not be touched"):
        reconcile_retired_branches(
            _PoisonConnection(),  # type: ignore[arg-type]
            name="acme/widgets",
            retired_branches=[None, "old-feature", ""],  # type: ignore[list-item]
        )


@pytest.mark.unit
def test_reconcile_counts_is_a_frozen_dataclass() -> None:
    counts = ReconcileCounts(branches_removed=1, files_stripped=2, files_deleted=3)
    assert (counts.branches_removed, counts.files_stripped, counts.files_deleted) == (1, 2, 3)
    with pytest.raises(AttributeError):
        counts.branches_removed = 9  # type: ignore[misc]
