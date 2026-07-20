"""Unit tests for indexer.resolve: filtering, dedup, fail-fast, success logging.

The two enumerators are injected, so every test here is HTTP-free -- the
``httpx.Client`` argument is never touched by a fake enumerator and is passed as
``None``. Enumeration itself is covered by ``tests/unit/test_fetch.py``.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import httpx
import pytest

from indexer.fetch import RepoMeta
from indexer.repo_config import RepoConfig
from indexer.resolve import EmptyConfigError, RepoCeilingError, resolve_repos

_NO_CLIENT = cast(httpx.Client, None)


def _meta(
    full_name: str, *, fork: bool = False, archived: bool = False, size_kb: int = 10
) -> RepoMeta:
    return RepoMeta(full_name=full_name, fork=fork, archived=archived, size_kb=size_kb)


def _config(*connections: dict[str, Any]) -> RepoConfig:
    return RepoConfig.model_validate(
        {"version": 1, "connections": [{"type": "github", **c} for c in connections]}
    )


class _Enumerator:
    """Records every call so AC 26 can assert explicit entries enumerate nothing."""

    def __init__(self, results: dict[str, list[RepoMeta]] | None = None) -> None:
        self.results = results or {}
        self.calls: list[str] = []

    def __call__(self, client: httpx.Client, selector: str) -> list[RepoMeta]:
        self.calls.append(selector)
        return self.results.get(selector, [])


def _resolve(config: RepoConfig, *, orgs: _Enumerator, users: _Enumerator, **kw: Any) -> list[str]:
    return resolve_repos(config, _NO_CLIENT, org_enumerator=orgs, user_enumerator=users, **kw)


# --- AC 22-25: the four exclude rules, applied to enumerated repos ----------


@pytest.mark.unit
@pytest.mark.parametrize("excluded", [True, False])
def test_exclude_forks(excluded: bool) -> None:
    orgs = _Enumerator({"acme": [_meta("acme/widgets"), _meta("acme/forked", fork=True)]})
    got = _resolve(
        _config({"orgs": ["acme"], "exclude": {"forks": excluded}}),
        orgs=orgs,
        users=_Enumerator(),
    )
    assert got == (["acme/widgets"] if excluded else ["acme/widgets", "acme/forked"])


@pytest.mark.unit
@pytest.mark.parametrize("excluded", [True, False])
def test_exclude_archived(excluded: bool) -> None:
    orgs = _Enumerator({"acme": [_meta("acme/widgets"), _meta("acme/old", archived=True)]})
    got = _resolve(
        _config({"orgs": ["acme"], "exclude": {"archived": excluded}}),
        orgs=orgs,
        users=_Enumerator(),
    )
    assert got == (["acme/widgets"] if excluded else ["acme/widgets", "acme/old"])


@pytest.mark.unit
def test_exclude_repos_globs_against_canonical_full_name() -> None:
    orgs = _Enumerator({"acme": [_meta("acme/test-harness"), _meta("acme/widgets")]})
    got = _resolve(
        _config({"orgs": ["acme"], "exclude": {"repos": ["acme/test-*"]}}),
        orgs=orgs,
        users=_Enumerator(),
    )
    assert got == ["acme/widgets"]


@pytest.mark.unit
def test_exclude_size_mb_compares_kb_against_mb_times_1000() -> None:
    orgs = _Enumerator(
        {"acme": [_meta("acme/huge", size_kb=200_000), _meta("acme/small", size_kb=50_000)]}
    )
    got = _resolve(
        _config({"orgs": ["acme"], "exclude": {"size_mb": 100}}),
        orgs=orgs,
        users=_Enumerator(),
    )
    assert got == ["acme/small"]


# --- AC 26-27: explicit repos bypass exclude and enumerate nothing ---------


@pytest.mark.unit
def test_explicit_repos_bypass_exclude_and_enumerate_nothing() -> None:
    orgs, users = _Enumerator(), _Enumerator()
    got = _resolve(
        _config({"repos": ["acme/test-thing"], "exclude": {"repos": ["acme/test-*"]}}),
        orgs=orgs,
        users=users,
    )
    assert got == ["acme/test-thing"]
    assert orgs.calls == []
    assert users.calls == []


@pytest.mark.unit
def test_explicit_repos_are_normalized() -> None:
    got = _resolve(
        _config({"repos": ["https://github.com/acme/w.git"]}),
        orgs=_Enumerator(),
        users=_Enumerator(),
    )
    assert got == ["acme/w"]


@pytest.mark.unit
def test_explicit_repo_bad_host_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported host"):
        _resolve(
            _config({"repos": ["https://evil.com/a/b"]}),
            orgs=_Enumerator(),
            users=_Enumerator(),
        )


# --- AC 28: dedup across selectors and connections, first-seen order -------


@pytest.mark.unit
def test_dedup_across_connections_and_selectors_preserves_first_seen_order() -> None:
    orgs = _Enumerator({"acme": [_meta("acme/widgets"), _meta("acme/gears")]})
    got = _resolve(
        _config(
            {"orgs": ["acme"], "repos": ["acme/widgets"]},
            {"orgs": ["acme"], "repos": ["acme/gears", "acme/extra"]},
        ),
        orgs=orgs,
        users=_Enumerator(),
    )
    assert got == ["acme/widgets", "acme/gears", "acme/extra"]


@pytest.mark.unit
def test_dedup_is_case_insensitive_and_keeps_first_seen_spelling() -> None:
    """GitHub repo names are case-insensitive, so the dedup key must be too.

    A hand-typed explicit entry rarely matches GitHub's canonical casing. Keying
    dedup on the raw string would emit both spellings, producing two `repos` rows
    for one repo and duplicating every one of its search hits, silently.
    """
    orgs = _Enumerator({"acme": [_meta("IceRhymers/MyRepo")]})
    got = _resolve(
        _config({"orgs": ["acme"], "repos": ["icerhymers/myrepo"]}),
        orgs=orgs,
        users=_Enumerator(),
    )
    # The enumerated (canonical) spelling was seen first, so it is what survives.
    assert got == ["IceRhymers/MyRepo"]


# --- AC 29: the two EmptyConfigError message shapes ------------------------


@pytest.mark.unit
def test_empty_because_nothing_enumerated() -> None:
    with pytest.raises(EmptyConfigError) as exc:
        _resolve(
            _config({"orgs": ["acme"], "users": ["nobody"]}),
            orgs=_Enumerator(),
            users=_Enumerator(),
        )
    message = str(exc.value)
    assert "0 of 0" in message
    assert "check org/user names and token scopes" in message


@pytest.mark.unit
def test_empty_because_everything_excluded_reports_tallies() -> None:
    enumerated = [_meta(f"acme/f{i}", fork=True) for i in range(31)]
    enumerated += [_meta(f"acme/a{i}", archived=True) for i in range(6)]
    with pytest.raises(EmptyConfigError) as exc:
        _resolve(
            _config({"orgs": ["acme"]}), orgs=_Enumerator({"acme": enumerated}), users=_Enumerator()
        )
    message = str(exc.value)
    assert "0 of 37" in message
    assert "forks=31" in message
    assert "archived=6" in message
    assert "check exclude rules" in message


# --- AC 30: per-connection tallies, cross-connection retention ------------


@pytest.mark.unit
def test_repo_dropped_in_one_connection_is_retained_via_another(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forked = _meta("acme/forked", fork=True)
    orgs = _Enumerator({"acme": [forked, _meta("acme/widgets")]})
    with caplog.at_level(logging.INFO, logger="indexer.resolve"):
        got = _resolve(
            _config(
                {"orgs": ["acme"], "exclude": {"forks": True}},
                {"orgs": ["acme"], "exclude": {"forks": False}},
            ),
            orgs=orgs,
            users=_Enumerator(),
        )
    # Retained by connection 1 even though connection 0 dropped it.
    assert got == ["acme/widgets", "acme/forked"]
    messages = [record.getMessage() for record in caplog.records]
    assert "connection 0 (github): enumerated 2, retained 1, explicit 0" in messages
    assert "connection 1 (github): enumerated 2, retained 2, explicit 0" in messages


# --- AC 31: bounded success logging ---------------------------------------


@pytest.mark.unit
def test_success_logging_lists_all_names_when_small(caplog: pytest.LogCaptureFixture) -> None:
    orgs = _Enumerator({"acme": [_meta("acme/widgets"), _meta("acme/gears")]})
    with caplog.at_level(logging.INFO, logger="indexer.resolve"):
        _resolve(
            _config({"orgs": ["acme"], "repos": ["acme/manual"]}),
            orgs=orgs,
            users=_Enumerator(),
        )
    messages = [record.getMessage() for record in caplog.records]
    assert "connection 0 (github): enumerated 2, retained 2, explicit 1" in messages
    assert "resolved 3 repos from 2 enumerated across 1 connection" in messages
    assert "resolved repos: acme/widgets, acme/gears, acme/manual" in messages


@pytest.mark.unit
def test_success_logging_truncates_names_above_fifty(caplog: pytest.LogCaptureFixture) -> None:
    enumerated = [_meta(f"acme/r{i:03d}") for i in range(60)]
    with caplog.at_level(logging.INFO, logger="indexer.resolve"):
        _resolve(
            _config({"orgs": ["acme"]}),
            orgs=_Enumerator({"acme": enumerated}),
            users=_Enumerator(),
        )
    names = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("resolved repos: ")
    )
    assert "acme/r000" in names
    assert "acme/r049" in names
    assert "acme/r050" not in names
    assert names.endswith("… and 10 more")


# --- AC 32: the ceiling, checked after dedup ------------------------------


@pytest.mark.unit
def test_above_ceiling_raises_with_count_ceiling_and_remedy() -> None:
    enumerated = [_meta(f"acme/r{i:03d}") for i in range(11)]
    with pytest.raises(RepoCeilingError) as exc:
        _resolve(
            _config({"orgs": ["acme"]}),
            orgs=_Enumerator({"acme": enumerated}),
            users=_Enumerator(),
            max_repos=10,
        )
    message = str(exc.value)
    assert "11" in message
    assert "10" in message
    assert "--max_repos" in message


@pytest.mark.unit
def test_ceiling_is_checked_after_dedup() -> None:
    enumerated = [_meta(f"acme/r{i:03d}") for i in range(10)]
    # 20 enumerated across two connections, deduping to 10 -- must pass at 10.
    got = _resolve(
        _config({"orgs": ["acme"]}, {"orgs": ["acme"]}),
        orgs=_Enumerator({"acme": enumerated}),
        users=_Enumerator(),
        max_repos=10,
    )
    assert len(got) == 10
