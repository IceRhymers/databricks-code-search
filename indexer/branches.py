"""Resolve a config connection's branch globs into a concrete per-repo branch list.

The default branch is always included, regardless of whether it matches a glob
-- a repo can never resolve to zero branches. Empty ``globs`` means
default-branch-only (``indexer.repo_config.GitHubConnection.branches``'
documented default), resolved WITHOUT consulting ``all_branches`` at all: the
caller (``indexer.job``) skips the GitHub branches API call entirely in that
common case.

:func:`resolve_branches` returns a :class:`BranchResolution`, not a bare list --
its ``complete`` flag is what lets corpus reconciliation (#56) trust a resolved
branch set as deletion evidence. A branch set truncated by the soft cap
(``complete=False``) must never be treated as the FULL set a repo resolves to:
reconciliation logic that deletes ``repo_branches`` rows or ``files`` membership
for anything "no longer resolved" would otherwise retire branches that were only
dropped by the cap, not genuinely removed upstream. Nothing in THIS module
performs that deletion -- it only makes the completeness of its own output an
unignorable, typed property so the caller (and #59's clean-run gate) can.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from fnmatch import fnmatchcase

logger = logging.getLogger("indexer.branches")

# A runaway config (a typo'd broad glob like `*`) must not fan a single repo out
# into an unbounded number of per-branch indexing runs. Overridable only by
# narrowing the globs in config.yaml -- there is no override flag, mirroring
# indexer.resolve.MAX_REPOS' philosophy of a config-level fix over a knob.
SOFT_BRANCH_CAP = 20


@dataclass(frozen=True)
class BranchResolution:
    """The outcome of resolving one repo's branch globs against its real branch list.

    ``branches`` keeps its historical shape and ordering (default-first, then
    alphabetical, capped). ``complete`` is ``True`` iff ``branches`` is the WHOLE
    set the globs resolve to -- ``False`` when the soft cap truncated it, in
    which case ``dropped`` names exactly what was left out, in the same
    alphabetical order as the kept list (the default branch is never dropped,
    so ``dropped`` is purely alphabetical, not default-first) and ``cap`` is
    the limit that was applied. ``dropped`` is empty and ``cap`` still reports the limit that was
    checked against (even when nothing was dropped), so a caller never has to
    special-case "no truncation happened" as `cap` being absent.
    """

    branches: list[str]
    complete: bool
    dropped: tuple[str, ...]
    cap: int


def resolve_branches(
    default_branch: str,
    all_branches: list[str],
    globs: list[str],
    *,
    repo: str,
    cap: int = SOFT_BRANCH_CAP,
) -> BranchResolution:
    """Resolve ``globs`` against ``all_branches`` for one repo.

    ``default_branch`` is always first in the result. The rest are every branch
    in ``all_branches`` matching any glob (``fnmatchcase`` -- a plain glob against
    the exact branch name, identically on every platform), deduped and sorted
    alphabetically. If the result exceeds ``cap``, it is truncated to ``cap``
    (default-first, alphabetical) with a loud warning naming ``repo``, the cap,
    and the dropped branches -- a truncation, not a failure, so indexing still
    proceeds on the kept branches. The warning also states that discovery is
    incomplete and reconciliation is blocked for this repo until the config is
    narrowed, because a truncated set is not evidence of the repo's full branch
    membership.

    An empty ``globs`` resolves to just ``[default_branch]`` with
    ``complete=True`` -- the default-branch-only fast path never consults
    ``all_branches`` and can never be truncated.
    """
    if not globs:
        return BranchResolution(branches=[default_branch], complete=True, dropped=(), cap=cap)

    matched = {b for b in all_branches if any(fnmatchcase(b, g) for g in globs)}
    matched.add(default_branch)

    rest = sorted(b for b in matched if b != default_branch)
    ordered = [default_branch, *rest]

    if len(ordered) > cap:
        dropped = tuple(ordered[cap:])
        logger.warning(
            "%s: resolved %d branches, above the soft cap of %d; dropping %d "
            "(keeping default-first, alphabetical), leaving discovery INCOMPLETE and "
            "reconciliation blocked for this repo until the config is narrowed: %s",
            repo,
            len(ordered),
            cap,
            len(dropped),
            ", ".join(dropped),
        )
        return BranchResolution(branches=ordered[:cap], complete=False, dropped=dropped, cap=cap)

    return BranchResolution(branches=ordered, complete=True, dropped=(), cap=cap)
