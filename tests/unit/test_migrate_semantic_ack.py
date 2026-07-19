"""The gated semantic migrate refuses to run without an explicit acknowledgement.

The guard lives in ``scripts/migrate.py::_require_semantic_ack`` rather than only in
the ``migrate-semantic`` Make recipe, because the recipe is not the only way in --
the runbook prints the raw ``scripts/migrate.py --semantic`` command and automation
can call it directly. These tests pin that the guard is enforced at the script level
and, critically, that it runs BEFORE any database connection is opened (no side
effects without consent).
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

import scripts.migrate as migrate


class _NotATty:
    """Stand-in for a non-interactive stdin (CI / automation)."""

    @staticmethod
    def isatty() -> bool:
        return False


class _Tty:
    @staticmethod
    def isatty() -> bool:
        return True


@pytest.mark.unit
def test_env_ack_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_SEARCH_SEMANTIC_ACK", "1")
    monkeypatch.setattr(sys, "stdin", _NotATty())
    migrate._require_semantic_ack()  # does not raise


@pytest.mark.unit
def test_non_tty_without_ack_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODE_SEARCH_SEMANTIC_ACK", raising=False)
    monkeypatch.setattr(sys, "stdin", _NotATty())
    with pytest.raises(RuntimeError, match="requires an explicit acknowledgement"):
        migrate._require_semantic_ack()


@pytest.mark.unit
def test_wrong_env_value_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODE_SEARCH_SEMANTIC_ACK", "yes")  # only "1" counts
    monkeypatch.setattr(sys, "stdin", _NotATty())
    with pytest.raises(RuntimeError, match="requires an explicit acknowledgement"):
        migrate._require_semantic_ack()


@pytest.mark.unit
def test_tty_accepts_the_exact_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODE_SEARCH_SEMANTIC_ACK", raising=False)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "enable-semantic")
    migrate._require_semantic_ack()  # does not raise


@pytest.mark.unit
def test_tty_rejects_a_wrong_phrase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODE_SEARCH_SEMANTIC_ACK", raising=False)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")
    with pytest.raises(RuntimeError, match="aborted at the confirmation prompt"):
        migrate._require_semantic_ack()


@pytest.mark.unit
def test_ack_is_checked_before_any_db_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_semantic must abort before create_db_engine -- no connection without consent."""
    monkeypatch.delenv("CODE_SEARCH_SEMANTIC_ACK", raising=False)
    monkeypatch.setattr(sys, "stdin", _NotATty())

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("create_db_engine must not be reached without an ack")

    monkeypatch.setattr(migrate, "create_db_engine", _explode)
    with pytest.raises(RuntimeError, match="requires an explicit acknowledgement"):
        migrate.run_semantic()
