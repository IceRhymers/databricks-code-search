"""Repo-config schema, parsing, and ``org/repo`` canonicalisation.

The central ``config.yaml`` declares which repositories the indexing job should
index, as a list of ``connections`` discriminated on ``type``. **The schema is
provider-extensible; the code is GitHub-only** -- ``github`` is the sole v1
variant, ``normalize_repo`` validates against GitHub hosts only, and there is no
``type``-keyed dispatch anywhere. Adding a provider means adding a union member
*and* a fetch implementation; it does not mean parameterising the canonicaliser
or growing a provider abstraction (see the spec's Non-Goals).

This module is deliberately **import-light**: pydantic, PyYAML, and the stdlib
only. No ``httpx`` (``indexer.fetch`` owns all GitHub HTTP), no SQLAlchemy, and
no module-level ``databricks-sdk`` import. Schema tests therefore stay fast and
dependency-free, and ``indexer.resolve`` can import ``normalize_repo`` from here
without the import cycle that living in ``indexer.job`` would create.

Reading the file needs a ``WorkspaceClient``, so the I/O is confined to
:func:`read_workspace_config`, which wraps *every* SDK failure in
:class:`ConfigError` carrying the path and any recoverable HTTP status -- 404
("never synced") must stay distinguishable from 403 ("no read permission").
:func:`parse_config` is pure: bytes in, model out.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_GITHUB_HOSTS = {"github.com", "www.github.com"}


def normalize_repo(entry: str) -> str:
    """Normalize a repo entry to canonical ``org/repo`` (the ``repos.name`` key).

    Accepts ``https://github.com/org/repo(.git)``, ``git@github.com:org/repo.git``,
    and bare ``org/repo``. Rejects other hosts, empty input, and anything that is
    not exactly two ``org/repo`` segments.
    """
    raw = entry.strip()
    if not raw:
        raise ValueError("empty repo entry")

    slug = raw
    if slug.startswith("git@"):
        # git@github.com:org/repo.git
        host, _, path = slug[len("git@") :].partition(":")
        if host not in _GITHUB_HOSTS:
            raise ValueError(f"unsupported host in repo entry: {entry!r}")
        slug = path
    elif "://" in slug:
        # https://github.com/org/repo(.git)
        scheme_host, _, path = slug.partition("://")[2].partition("/")
        if scheme_host not in _GITHUB_HOSTS:
            raise ValueError(f"unsupported host in repo entry: {entry!r}")
        slug = path

    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    slug = slug.strip("/")

    if not _REPO_RE.match(slug):
        raise ValueError(f"could not parse a github org/repo from {entry!r}")
    # Reject degenerate `.`/`..` segments GitHub itself rejects; keeps them out of
    # the API URL path and the tarball dest even though the host is fixed.
    if any(part in {".", ".."} for part in slug.split("/")):
        raise ValueError(f"invalid org/repo segment in {entry!r}")
    return slug


class ExcludeRules(BaseModel):
    """Filters applied to repos discovered via ``orgs`` / ``users`` enumeration.

    **``exclude`` does NOT apply to explicitly listed ``repos``** -- naming a repo
    by hand is an unambiguous instruction, and explicit always wins. Delete the
    line to drop an explicit repo.

    ``size_mb`` is compared against GitHub's reported repo ``size``, which is the
    **git directory in KB** (history included), so the comparison is
    ``size_kb > size_mb * 1000``. It is not a bound on the HEAD tarball.
    """

    forks: bool = True
    archived: bool = True
    repos: list[str] = []
    size_mb: int | None = None


class GitHubConnection(BaseModel):
    """A GitHub selector set. ``orgs``, ``users``, and ``repos`` are unioned."""

    type: Literal["github"]
    orgs: list[str] = []
    users: list[str] = []
    repos: list[str] = []
    exclude: ExcludeRules = ExcludeRules()

    @model_validator(mode="after")
    def _require_a_selector(self) -> GitHubConnection:
        if not (self.orgs or self.users or self.repos):
            raise ValueError("connection selects nothing: set at least one of orgs, users, repos")
        return self


# A single-member discriminated union today. Declared as an alias so a future
# `GitLabConnection` is a one-token edit -- NOT a dispatch point (see docstring).
Connection = Annotated[GitHubConnection, Field(discriminator="type")]


class RepoConfig(BaseModel):
    """The parsed ``config.yaml`` document."""

    version: Literal[1]
    connections: list[Connection] = Field(min_length=1)


class ConfigError(Exception):
    """Config could not be read or parsed. Carries the source path."""


# SDK exception classes do not expose the HTTP status as an attribute, so fall
# back to the class name. Kept local: importing databricks-sdk here would break
# this module's import-light property.
_SDK_STATUS_BY_NAME = {
    "BadRequest": 400,
    "Unauthenticated": 401,
    "PermissionDenied": 403,
    "NotFound": 404,
    "ResourceConflict": 409,
    "TooManyRequests": 429,
    "InternalError": 500,
    "TemporarilyUnavailable": 503,
    "DeadlineExceeded": 504,
}


def _http_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status for an SDK/transport exception, or ``None``."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    return _SDK_STATUS_BY_NAME.get(type(exc).__name__)


def parse_config(raw: bytes, *, source: str) -> RepoConfig:
    """Parse ``config.yaml`` bytes into a :class:`RepoConfig`. Pure -- no I/O."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config at {source!r} is not valid UTF-8: {exc}") from exc

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config at {source!r} is not valid YAML: {exc}") from exc

    if doc is None:
        raise ConfigError(
            f"config at {source!r} is empty; "
            "expected a mapping with 'version' and 'connections' keys"
        )
    if not isinstance(doc, dict):
        raise ConfigError(
            f"config at {source!r} must be a YAML mapping, got {type(doc).__name__}; "
            "expected top-level 'version' and 'connections' keys"
        )

    try:
        return RepoConfig.model_validate(doc)
    except ValidationError as exc:
        raise ConfigError(f"config at {source!r} is invalid: {exc}") from exc


def read_workspace_config(client: Any, path: str) -> bytes:
    """Download ``path`` from the Databricks workspace as raw bytes.

    Every failure is wrapped in :class:`ConfigError` naming the path and, when
    recoverable, the HTTP status -- an escaping SDK ``NotFound`` would bypass the
    job's config handler and break its exit-code contract. The status is what
    lets an operator tell "never synced" (404) from "no read permission" (403).
    """
    try:
        with client.workspace.download(path) as fh:
            data = fh.read()
    except Exception as exc:
        status = _http_status(exc)
        where = f"HTTP {status}" if status is not None else type(exc).__name__
        raise ConfigError(f"failed to read config from {path!r} ({where}): {exc}") from exc
    return bytes(data)


def load_config(client: Any, path: str) -> RepoConfig:
    """Read ``path`` from the workspace and parse it."""
    return parse_config(read_workspace_config(client, path), source=path)
