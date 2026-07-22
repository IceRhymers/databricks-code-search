"""Fetch a GitHub repo as a tarball over HTTP — no git binary (serverless-safe).

The caller sets the ``Authorization`` header on the :class:`httpx.Client`; this
module never reads secrets itself. ``download_tarball`` is always called with the
immutable resolved ``sha`` (not a branch name) so the extracted tree's SHA can
never drift from the ``head_sha`` stamped into ``files.commit``.
"""

from __future__ import annotations

import logging
import shutil
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("indexer.fetch")

_API_BASE = "https://api.github.com"
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Defense-in-depth caps for the untrusted tarball on an ephemeral serverless disk:
# bound the compressed download and the uncompressed extraction independently so a
# gzip bomb or an oversized tracked blob can't exhaust local storage.
MAX_TARBALL_BYTES = 500_000_000
MAX_EXTRACTED_BYTES = 2_000_000_000

# One worker's worst-case peak. The two caps SUM rather than max(): the compressed
# tarball stays on disk inside the worker's TemporaryDirectory while the extracted
# tree grows beside it, so both are alive simultaneously.
REQUIRED_FREE_BYTES = MAX_TARBALL_BYTES + MAX_EXTRACTED_BYTES

_PER_PAGE = 100


@dataclass(frozen=True)
class RepoMeta:
    """The subset of GitHub's list-repos object the resolution layer filters on.

    ``size_kb`` is GitHub's ``size`` field, which reports the **git directory** in
    KB (history included) — not the size of a tarball of HEAD.
    """

    full_name: str
    fork: bool
    archived: bool
    size_kb: int


class RateLimitError(Exception):
    """A genuine GitHub rate-limit / secondary-limit response.

    Deliberately narrow: GitHub also answers 403 for permission failures (a PAT
    without org scope), and mislabeling those as quota failures would send the
    operator off to wait for a reset that never helps.
    """


def _rate_limit_reason(response: httpx.Response) -> str | None:
    """Return a human wait/reset description iff this response is a *real* rate limit.

    429 always counts. 403 counts only when ``Retry-After`` is present or
    ``X-RateLimit-Remaining`` is exactly ``0``; every other 403 is an ordinary
    permission failure and must fall through to ``raise_for_status()``.
    ``Retry-After`` takes precedence over ``X-RateLimit-Reset``.
    """
    if response.status_code not in (403, 429):
        return None

    retry_after = response.headers.get("Retry-After")
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset = response.headers.get("X-RateLimit-Reset")

    if response.status_code == 403 and retry_after is None and remaining != "0":
        return None

    if retry_after is not None:
        return f"retry after {retry_after}s"
    if reset is not None:
        try:
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(reset)))
        except ValueError:
            return f"rate limit resets at {reset}"
        return f"rate limit resets at {when}"
    return "no reset time reported"


def _has_next_page(link_header: str | None) -> bool:
    """True when the ``Link`` header advertises a ``rel="next"`` page.

    Only ``next`` terminates the loop's continuation; ``prev``/``last``/``first``
    rels are present on GitHub's last page and must not be mistaken for one.
    """
    if not link_header:
        return False
    for link in link_header.split(","):
        for param in link.split(";")[1:]:
            key, _, value = param.strip().partition("=")
            if key == "rel" and value.strip('"') == "next":
                return True
    return False


def _paginated_get(client: httpx.Client, url: str, *, selector: str) -> list[dict[str, Any]]:
    """Page through a GitHub list endpoint, following ``Link`` rel=next.

    Item-shape-agnostic: returns raw decoded JSON objects, one list per page
    concatenated. ``selector`` names what is being paged (an org/user, or an
    ``org/repo`` for a branch listing) in every :class:`RateLimitError` and the
    completion log line, so a quota failure or a rate-limit record points at its
    own cause. Request headers are never logged (token-redaction invariant).
    """
    items: list[dict[str, object]] = []
    page = 1
    remaining: str | None = None

    while True:
        response = client.get(
            url,
            headers=_GITHUB_HEADERS,
            params={"per_page": _PER_PAGE, "page": page},
        )
        reason = _rate_limit_reason(response)
        if reason is not None:
            raise RateLimitError(
                f"GitHub rate limit hit while enumerating {selector} "
                f"(HTTP {response.status_code}); {reason}"
            )
        response.raise_for_status()

        remaining = response.headers.get("X-RateLimit-Remaining")
        items.extend(response.json())

        if not _has_next_page(response.headers.get("Link")):
            break
        page += 1

    logger.info(
        "enumerated %d for %s; GitHub rate limit remaining: %s",
        len(items),
        selector,
        remaining if remaining is not None else "unknown",
    )
    return items


def _list_repos(client: httpx.Client, url: str, *, selector: str) -> list[RepoMeta]:
    """Page through a GitHub list-repos endpoint, following ``Link`` rel=next."""
    return [
        RepoMeta(
            full_name=str(item["full_name"]),
            fork=bool(item["fork"]),
            archived=bool(item["archived"]),
            size_kb=int(item["size"]),
        )
        for item in _paginated_get(client, url, selector=selector)
    ]


def list_org_repos(client: httpx.Client, org: str) -> list[RepoMeta]:
    """Every repo visible to the token under organization ``org``."""
    return _list_repos(client, f"{_API_BASE}/orgs/{org}/repos", selector=org)


def list_user_repos(client: httpx.Client, user: str) -> list[RepoMeta]:
    """Every repo visible to the token owned by ``user``."""
    return _list_repos(client, f"{_API_BASE}/users/{user}/repos", selector=user)


def list_branches(client: httpx.Client, org: str, repo: str) -> list[str]:
    """Every branch name for ``org/repo``, paginated the same way as list-repos.

    Used only when a connection configures ``branches:`` globs (plan Phase 2):
    resolving glob patterns needs the repo's real branch list to match against.

    Complete-or-raise, via :func:`_paginated_get`: any page's failure (a rate
    limit, an HTTP error, anything else) raises out of this call rather than
    returning whatever pages were fetched so far -- there is no partial-list
    return path. That is load-bearing for corpus reconciliation (#56): a caller
    that resolved a truncated branch list without knowing it was truncated could
    treat missing branches as evidence they no longer exist upstream and retire
    them. A mid-pagination failure here instead fails the whole repo (see
    ``indexer.job._index_one_inner``), never a silently short branch list.
    """
    items = _paginated_get(
        client, f"{_API_BASE}/repos/{org}/{repo}/branches", selector=f"{org}/{repo} branches"
    )
    return [str(item["name"]) for item in items]


def resolve_branch_head(client: httpx.Client, org: str, repo: str, branch: str) -> str:
    """The immutable HEAD SHA of ``branch``.

    The tarball is always downloaded by this SHA, never by the branch name.
    """
    commit = client.get(
        f"{_API_BASE}/repos/{org}/{repo}/commits/{branch}",
        headers=_GITHUB_HEADERS,
    )
    commit.raise_for_status()
    return str(commit.json()["sha"])


def resolve_ref(client: httpx.Client, org: str, repo: str) -> tuple[str, str]:
    """Return ``(default_branch, head_sha)`` for ``org/repo``.

    Two calls: the repo metadata for the default branch, then
    :func:`resolve_branch_head` for that branch's immutable HEAD SHA.
    """
    meta = client.get(f"{_API_BASE}/repos/{org}/{repo}", headers=_GITHUB_HEADERS)
    meta.raise_for_status()
    default_branch = str(meta.json()["default_branch"])
    head_sha = resolve_branch_head(client, org, repo, default_branch)
    return default_branch, head_sha


def assert_disk_headroom(path: Path, *, repo: str) -> None:
    """Raise ``OSError`` unless ``path``'s filesystem can hold one worker's peak.

    Called immediately before the download, on the directory actually being
    written to, so the measurement is of the right filesystem. The error names
    the repo AND the config key to lower, because the alternative -- an opaque
    ENOSPC from somewhere inside tarfile -- says nothing about which of N
    concurrent workers overcommitted the disk or what to do about it.

    The caller is expected to let this fail ONE repo, not the run: with a
    too-high ``index_concurrency`` the run then degrades to indexing whatever
    fits rather than losing everything.
    """
    free = shutil.disk_usage(path).free
    if free < REQUIRED_FREE_BYTES:
        raise OSError(
            f"insufficient local disk for {repo}: {free} bytes free at {path}, need "
            f"{REQUIRED_FREE_BYTES} (a {MAX_TARBALL_BYTES}-byte tarball plus a "
            f"{MAX_EXTRACTED_BYTES}-byte extraction, both alive at once); "
            "lower index_concurrency in config.yaml"
        )


def download_tarball(client: httpx.Client, org: str, repo: str, ref: str, dest: Path) -> Path:
    """Stream the ``org/repo`` tarball at immutable ``ref`` to ``dest/source.tar.gz``.

    ``ref`` MUST be the resolved SHA, never a branch: a push between resolve and
    download would otherwise yield a tree whose real SHA differs from the stamped
    ``head_sha`` and corrupt the mark-and-sweep key. GitHub answers with a 302 to
    codeload, so ``follow_redirects=True`` is required.
    """
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "source.tar.gz"
    url = f"{_API_BASE}/repos/{org}/{repo}/tarball/{ref}"
    with client.stream("GET", url, headers=_GITHUB_HEADERS, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = 0
        with out.open("wb") as fh:
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > MAX_TARBALL_BYTES:
                    raise ValueError(
                        f"tarball for {org}/{repo} exceeds {MAX_TARBALL_BYTES} bytes "
                        f"by {total - MAX_TARBALL_BYTES} bytes; "
                        f"consider exclude.size_mb in config.yaml"
                    )
                fh.write(chunk)
    return out


def extract_tarball(tar_path: Path, dest: Path) -> Path:
    """Safely extract ``tar_path`` into ``dest`` and return its single top-level dir.

    ``filter="data"`` (Python 3.12) neutralizes path traversal / absolute paths /
    device files. GitHub tarballs contain exactly one top-level ``org-repo-<sha7>/``
    directory.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, mode="r:*") as tf:
        members = tf.getmembers()
        # Reject a decompression bomb before writing any of it to disk.
        extracted = sum(m.size for m in members if m.isreg())
        if extracted > MAX_EXTRACTED_BYTES:
            raise ValueError(
                f"tarball extracts to {extracted} bytes, exceeding {MAX_EXTRACTED_BYTES}"
            )
        tf.extractall(dest, filter="data")

    top_level = {
        member.name.split("/", 1)[0]
        for member in members
        if member.name and not member.name.startswith("/")
    }
    if len(top_level) != 1:
        raise ValueError(
            f"expected exactly one top-level dir in tarball, found {sorted(top_level)}"
        )
    return dest / next(iter(top_level))
