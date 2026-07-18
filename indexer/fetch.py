"""Fetch a GitHub repo as a tarball over HTTP — no git binary (serverless-safe).

The caller sets the ``Authorization`` header on the :class:`httpx.Client`; this
module never reads secrets itself. ``download_tarball`` is always called with the
immutable resolved ``sha`` (not a branch name) so the extracted tree's SHA can
never drift from the ``head_sha`` stamped into ``files.commit``.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import httpx

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


def resolve_ref(client: httpx.Client, org: str, repo: str) -> tuple[str, str]:
    """Return ``(default_branch, head_sha)`` for ``org/repo``.

    Two calls: the repo metadata for the default branch, then that branch's HEAD
    commit for the immutable SHA the tarball is downloaded by.
    """
    meta = client.get(f"{_API_BASE}/repos/{org}/{repo}", headers=_GITHUB_HEADERS)
    meta.raise_for_status()
    default_branch = str(meta.json()["default_branch"])

    commit = client.get(
        f"{_API_BASE}/repos/{org}/{repo}/commits/{default_branch}",
        headers=_GITHUB_HEADERS,
    )
    commit.raise_for_status()
    head_sha = str(commit.json()["sha"])
    return default_branch, head_sha


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
                    raise ValueError(f"tarball for {org}/{repo} exceeds {MAX_TARBALL_BYTES} bytes")
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
