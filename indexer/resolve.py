"""Resolve a :class:`~indexer.repo_config.RepoConfig` into canonical ``org/repo`` names.

Enumerate every ``orgs``/``users`` selector, filter the *enumerated* set through
that connection's ``ExcludeRules``, add explicit ``repos`` entries **unfiltered**
(explicit always wins), dedup across connections in first-seen order, and
fail-fast on an empty or oversized result -- all before the job downloads a
single tarball.

``normalize_repo`` is imported from :mod:`indexer.repo_config`, never from
:mod:`indexer.job`: ``indexer.job`` imports :func:`resolve_repos` from here, so
the reverse edge would be an import cycle that kills the wheel entry point at
import time with no diagnostic.

The two ``Enumerator`` parameters are a **test seam only** -- plain callables
default-bound to the concrete GitHub functions. There is no registry and no
``type``-keyed dispatch; the schema is provider-extensible, the code is not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from fnmatch import fnmatchcase

import httpx

from indexer.fetch import RepoMeta, list_org_repos, list_user_repos
from indexer.repo_config import GitHubConnection, RepoConfig, normalize_repo

logger = logging.getLogger("indexer.resolve")

# Runaway-config guardrail (a typo'd broad selector, an org with thousands of
# repos), not a wall-clock control -- a repo count cannot bound run time.
# Overridable without a code change via the job's --max_repos parameter.
MAX_REPOS = 500

# How many resolved names a single success log record may carry.
_MAX_LOGGED_NAMES = 50

# Test seam only -- NOT a provider-dispatch point (see module docstring).
Enumerator = Callable[[httpx.Client, str], list[RepoMeta]]


class EmptyConfigError(Exception):
    """The config resolved to zero repos. Indexing nothing must not exit 0."""


class RepoCeilingError(Exception):
    """The config resolved to more repos than the safety ceiling allows."""


def _drop_reason(meta: RepoMeta, connection: GitHubConnection) -> str | None:
    """The name of the first ``exclude`` rule that drops ``meta``, or ``None``.

    Applied to enumerated repos only -- explicit ``repos`` entries bypass all
    four rules. ``size_mb`` compares against GitHub's KB-denominated ``size``.
    """
    rules = connection.exclude
    if rules.forks and meta.fork:
        return "forks"
    if rules.archived and meta.archived:
        return "archived"
    # fnmatchcase, not fnmatch: fnmatch applies os.path.normcase, which would make
    # this glob case-insensitive on Windows and case-sensitive elsewhere. The
    # contract is a plain glob against the canonical org/repo, identically everywhere.
    if any(fnmatchcase(meta.full_name, pattern) for pattern in rules.repos):
        return "repos"
    if rules.size_mb is not None and meta.size_kb > rules.size_mb * 1000:
        return "size_mb"
    return None


def _format_names(names: list[str]) -> str:
    """Render resolved names for one log record, bounded at 50 plus a remainder."""
    if len(names) <= _MAX_LOGGED_NAMES:
        return ", ".join(names)
    head = ", ".join(names[:_MAX_LOGGED_NAMES])
    return f"{head} … and {len(names) - _MAX_LOGGED_NAMES} more"


def resolve_repos(
    config: RepoConfig,
    http_client: httpx.Client,
    *,
    org_enumerator: Enumerator = list_org_repos,
    user_enumerator: Enumerator = list_user_repos,
    max_repos: int = MAX_REPOS,
) -> list[str]:
    """Resolve ``config`` into a deduped, ordered list of canonical ``org/repo``.

    Enumeration exceptions propagate untouched -- nothing has been indexed yet,
    and Principle 3 says fail loudly before touching anything.

    Raises:
        EmptyConfigError: the config resolved to zero repos.
        RepoCeilingError: the config resolved to more than ``max_repos`` repos.
        ValueError: an explicit ``repos`` entry is not a valid GitHub repo.
    """
    resolved: list[str] = []
    seen: set[str] = set()
    total_enumerated = 0
    # (index, enumerated, retained, explicit, tallies) per connection.
    summaries: list[tuple[int, int, int, int, dict[str, int]]] = []

    for index, connection in enumerate(config.connections):
        enumerated: list[RepoMeta] = []
        for org in connection.orgs:
            enumerated.extend(org_enumerator(http_client, org))
        for user in connection.users:
            enumerated.extend(user_enumerator(http_client, user))

        tallies: dict[str, int] = {}
        retained: list[str] = []
        for meta in enumerated:
            reason = _drop_reason(meta, connection)
            if reason is not None:
                # Tallied per connection: a repo dropped here may still be
                # retained by another connection and appear in the result.
                tallies[reason] = tallies.get(reason, 0) + 1
                continue
            retained.append(meta.full_name)

        # Explicit entries bypass every exclude rule -- naming a repo by hand is
        # an unambiguous instruction, and it costs zero enumeration calls.
        explicit = [normalize_repo(entry) for entry in connection.repos]

        for name in [*retained, *explicit]:
            # Dedup case-INSENSITIVELY while keeping the first-seen spelling.
            # GitHub repo names are case-insensitive, so a hand-typed explicit
            # entry (`icerhymers/MyRepo`) and the canonical enumerated spelling
            # (`IceRhymers/MyRepo`) are the same repo; keying on the raw string
            # would index it twice and duplicate every one of its search hits.
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            resolved.append(name)

        total_enumerated += len(enumerated)
        summaries.append((index, len(enumerated), len(retained), len(explicit), tallies))

    connection_word = "connection" if len(config.connections) == 1 else "connections"

    if not resolved:
        if total_enumerated == 0:
            raise EmptyConfigError(
                f"resolved 0 of 0 repos across {len(config.connections)} {connection_word}; "
                "no selector returned any repository "
                "(check org/user names and token scopes)"
            )
        merged: dict[str, int] = {}
        for _, _, _, _, tallies in summaries:
            for rule, count in tallies.items():
                merged[rule] = merged.get(rule, 0) + count
        excluded = ", ".join(f"{rule}={count}" for rule, count in sorted(merged.items()))
        raise EmptyConfigError(
            f"resolved 0 of {total_enumerated} repos across "
            f"{len(config.connections)} {connection_word} (excluded: {excluded}); "
            "check exclude rules in config.yaml"
        )

    # After dedup, deliberately: 600 enumerated collapsing to 400 must pass.
    if len(resolved) > max_repos:
        raise RepoCeilingError(
            f"config resolved {len(resolved)} repos, above the ceiling of {max_repos}; "
            "narrow the selectors in config.yaml or raise --max_repos "
            "(the max_repos parameter in resources/job.yml)"
        )

    for index, enumerated_count, retained_count, explicit_count, _ in summaries:
        logger.info(
            "connection %d (github): enumerated %d, retained %d, explicit %d",
            index,
            enumerated_count,
            retained_count,
            explicit_count,
        )
    logger.info(
        "resolved %d repos from %d enumerated across %d %s",
        len(resolved),
        total_enumerated,
        len(config.connections),
        connection_word,
    )
    logger.info("resolved repos: %s", _format_names(resolved))
    return resolved
