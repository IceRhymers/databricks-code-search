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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

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
    """A GitHub selector set. ``orgs``, ``users``, and ``repos`` are unioned.

    ``branches`` is a list of glob patterns (Sourcebot ``revisions.branches``
    style) matched against each resolved repo's branch list, in addition to its
    default branch (always included regardless of match). **Empty (the default)
    means default-branch-only** -- the pre-multi-branch behavior every existing
    config keeps without a change. See ``indexer.branches.resolve_branches`` for
    the match/dedup/cap semantics.
    """

    type: Literal["github"]
    orgs: list[str] = []
    users: list[str] = []
    repos: list[str] = []
    branches: list[str] = []
    exclude: ExcludeRules = ExcludeRules()

    @model_validator(mode="after")
    def _require_a_selector(self) -> GitHubConnection:
        if not (self.orgs or self.users or self.repos):
            raise ValueError("connection selects nothing: set at least one of orgs, users, repos")
        return self


# A single-member discriminated union today. Declared as an alias so a future
# `GitLabConnection` is a one-token edit -- NOT a dispatch point (see docstring).
Connection = Annotated[GitHubConnection, Field(discriminator="type")]


class SemanticOverrides(BaseModel):
    """config.yaml's overlay onto the semantic knobs in ``app.config.Settings``.

    The serverless job has **no reachable env surface**: nothing in
    ``resources/job.yml`` sets ``CODE_SEARCH_*``, so every ``Settings`` semantic
    default is effectively hard-coded for the job. This block is how an operator
    moves those knobs from config.yaml -- the one file the job already reads --
    without editing bundle resources and redeploying. Precedence for the job is
    **config.yaml > ``CODE_SEARCH_*`` env > code default**: a field set here wins,
    an unset field (``None``) falls through to whatever the env/``Settings``
    default already resolved to. An absent ``semantic:`` block is a pure no-op, so
    every config predating this schema keeps exactly today's behavior.

    Every field is ``None`` by default -- "not set", NOT "set to false/zero". That
    is load-bearing: ``settings_overrides`` emits a key only for a field the
    operator actually wrote, so ``model_copy(update=...)`` in ``indexer.job``
    touches only those, and an omitted field is never silently forced to a
    default that would clobber the env value it was meant to inherit.

    The env surface (``app.config.get_settings``) is deliberately left as the MCP
    server / webui surface -- those processes have their own environment and this
    block does not reach them. See ``docs/runbooks/semantic-enablement.md``.

    **``max_chunks_per_repo`` here is the GLOBAL ceiling** (one int, the job-wide
    default), distinct from ``RepoConfig.semantic_max_chunks_per_repo`` -- a
    *map* of per-repo overrides. The two are intentionally similarly named because
    they tune the same dimension at different scopes; the per-repo map still beats
    this global from ANY source, because ``indexer.job`` resolves the effective cap
    as ``entry.semantic_max_chunks or cfg.semantic_max_chunks_per_repo`` and this
    block only moves the second operand. Setting both is coherent: raise the floor
    for the whole corpus here, spot-override the outliers in the map.

    Two ``Settings`` semantic knobs are deliberately absent, because exposing them
    would be a lie or a footgun: ``semantic_embedding_dim`` is pinned to
    ``SEMANTIC_EMBEDDING_DIM`` (the ``chunks.embedding`` column type and the 0004
    migration DDL both derive from it, tripwire-tested) -- it is a schema
    invariant, not an operator knob; and ``semantic_chunk_max_tokens`` is not
    consumed by the chunker at all (chunking is bounded by the
    ``SEMANTIC_CHUNK_MAX_CHARS`` constant), so a config key for it would silently
    do nothing.

    Validation mirrors the existing ``index_concurrency`` / per-repo-cap pattern:
    the bound lives on the field (``ge`` / ``gt`` / ``min_length``), so a bad value
    fails at parse time with pydantic's own message and reaches the job as a
    :class:`ConfigError` naming the source path -- never a mid-run surprise.

    ``extra="forbid"`` (unlike the extra-ignore default the rest of this schema
    keeps) is a FAIL-CLOSED guard, not tidiness: this block is the job's only
    semantic kill switch, and pydantic's default would turn a typo'd key
    (``enable: false`` for ``enabled: false``) into a silently ignored no-op that
    leaves semantic indexing ON -- fail-OPEN on exactly the surface that must fail
    loud. An unknown key here raises at parse time instead.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    max_chunks_per_repo: int | None = Field(default=None, ge=1)
    embedding_endpoint: str | None = Field(default=None, min_length=1)
    embedding_model: str | None = Field(default=None, min_length=1)
    embedding_batch_size: int | None = Field(default=None, ge=1)
    embedding_timeout_s: float | None = Field(default=None, gt=0)

    @field_validator("embedding_endpoint")
    @classmethod
    def _require_workspace_relative_path(cls, value: str | None) -> str | None:
        """Reject anything but a workspace-relative path (an SSRF / token-exfil guard).

        ``app.embed`` builds the request URL by bare string concatenation
        (``f"{host}{path}"`` on the SDK raw client), so a value like
        ``@evil.com/x`` would parse as ``userinfo@evil.com`` and ship the
        workspace bearer token to an attacker-controlled host, and an absolute
        ``https://evil/x`` or protocol-relative ``//evil/x`` would leave the
        workspace entirely. This is a config-surface guard only -- ``app.embed``
        and ``Settings`` are unchanged; the check lives here because config.yaml is
        operator-editable and this is where a bad value is cheapest to catch.
        """
        if value is None:
            return value
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError(
                "embedding_endpoint must be a workspace-relative path starting with a single '/' "
                f"(got {value!r})"
            )
        if "@" in value or "://" in value:
            raise ValueError(
                "embedding_endpoint must not contain '@' or '://' -- a workspace-relative path "
                f"only, or the workspace token could be sent off-host (got {value!r})"
            )
        return value

    def settings_overrides(self) -> dict[str, Any]:
        """Return the ``{Settings_field_name: value}`` overlay for the SET fields.

        The keys are ``app.config.Settings`` field names, spelled as string
        literals ON PURPOSE: importing ``app.config`` here would break this
        module's import-light contract (see the module docstring) and drag
        pydantic-settings into the fast schema tests. The literals are pinned to
        the real ``Settings`` fields by a type-aware tripwire in
        ``tests/unit/test_repo_config.py`` (it round-trips this dict through
        ``Settings.model_copy`` + ``model_validate``), so a rename on either side
        fails loudly rather than silently dropping an override.

        Only fields the operator actually set (non-``None``) are emitted, so the
        job's ``model_copy(update=...)`` overlays exactly those and leaves every
        other ``Settings`` value -- env-sourced or default -- untouched.
        """
        mapping = {
            "enabled": "semantic_enabled",
            "max_chunks_per_repo": "semantic_max_chunks_per_repo",
            "embedding_endpoint": "semantic_embedding_endpoint",
            "embedding_model": "semantic_embedding_model",
            "embedding_batch_size": "semantic_embedding_batch_size",
            "embedding_timeout_s": "semantic_embedding_timeout_s",
        }
        return {
            settings_field: value
            for local_field, settings_field in mapping.items()
            if (value := getattr(self, local_field)) is not None
        }


class RepoConfig(BaseModel):
    """The parsed ``config.yaml`` document.

    ``index_concurrency`` is how many repos the indexing job works on at once.
    **The default of 4 is a disk bound, not a CPU one.** Each in-flight worker
    holds ``MAX_TARBALL_BYTES`` (500 MB) *and* ``MAX_EXTRACTED_BYTES`` (2 GB)
    alive simultaneously -- the downloaded tarball stays inside the worker's
    ``TemporaryDirectory`` while the extraction runs beside it, so peak usage is
    2.5 GB per worker: 10 GB at the default 4, 20 GB at the ceiling of 8.

    **Returns at the ceiling are sublinear.** Symbol extraction does not
    parallelise (measured at 0.95x on 4 threads), so Amdahl's law caps the
    speedup well below 8x while the disk cost stays a hard linear 20 GB. Raise
    it only knowing that trade.

    When semantic indexing is on, the effective worker count is clamped to 2 by
    :func:`effective_workers`. That clamp is a **memory** bound, not a CPU one:
    embedding materialises a whole repo's chunks in memory (~0.5-0.8 GB per
    worker; 260 MB of vectors alone at the 8000-chunk ceiling).

    ``semantic_max_chunks_per_repo`` (the per-repo MAP) overrides that global
    8000-chunk ceiling for individual repos named here, without moving the global
    default. It does NOT relax the 2-worker semantic clamp above -- a large
    override still multiplies the per-worker memory cost of whichever of the (at
    most 2) concurrent semantic workers happens to be indexing that repo.

    The similarly-named ``semantic.max_chunks_per_repo`` (inside the ``semantic:``
    block, a single INT) moves that GLOBAL ceiling itself for the whole job. The
    two coexist by design: this map spot-overrides outliers, ``semantic:`` raises
    the floor everyone inherits. The map still wins for a repo it names, because
    the job resolves ``entry.semantic_max_chunks or cfg.semantic_max_chunks_per_repo``
    and the ``semantic:`` block only supplies the second operand -- see
    :class:`SemanticOverrides`.
    """

    version: Literal[1]
    connections: list[Connection] = Field(min_length=1)
    index_concurrency: int = Field(default=4, ge=1, le=8)

    # Per-repo override of Settings.semantic_max_chunks_per_repo (app/config.py, default
    # 8000), keyed by repo. Absent (the default) -> no repo gets an override, and
    # indexer.resolve.resolve_repos leaves RepoEntry.semantic_max_chunks at None, which
    # indexer/job.py reads as "use the global cap" -- an unmodified config is unaffected.
    # `ge=1` on the value type (not a separate validator) rejects 0/negative/non-int with
    # pydantic's own message, matching index_concurrency's pattern above.
    semantic_max_chunks_per_repo: dict[str, Annotated[int, Field(ge=1)]] = {}

    # The `semantic:` overlay onto the job's semantic Settings (config.yaml > env >
    # default). Absent (the default `SemanticOverrides()`) is a no-op: every field is
    # None, so `settings_overrides()` is empty and indexer/job.py never model_copies
    # the injected cfg. This is the JOB's config surface; env vars remain the MCP /
    # webui surface. See SemanticOverrides for the precedence and the deliberate
    # exclusions (embedding_dim, chunk_max_tokens).
    semantic: SemanticOverrides = SemanticOverrides()

    @model_validator(mode="after")
    def _normalize_semantic_overrides(self) -> RepoConfig:
        """Canonicalise override keys through ``normalize_repo`` and reject collisions.

        Runs at config-parse time, not at resolve time: a typo'd URL-style key
        (``https://github.com/acme/widgets``) must canonicalise to the same
        ``org/repo`` the corpus resolves to, or the override would silently never
        match. Two keys that only differ by case (``Acme/Widgets`` vs
        ``acme/widgets``) are the same GitHub repo, so YAML's own case-sensitive
        mapping keys would otherwise let both through and silently pick one.
        """
        normalized: dict[str, int] = {}
        seen_casefold: set[str] = set()
        for raw_key, cap in self.semantic_max_chunks_per_repo.items():
            key = normalize_repo(raw_key)
            folded = key.casefold()
            if folded in seen_casefold:
                raise ValueError(
                    "semantic_max_chunks_per_repo has duplicate keys for "
                    f"{key!r} (case-insensitively)"
                )
            seen_casefold.add(folded)
            normalized[key] = cap
        self.semantic_max_chunks_per_repo = normalized
        return self


def effective_workers(config: RepoConfig, *, semantic_enabled: bool) -> int:
    """Worker-pool size for a run, applying the semantic memory clamp.

    Takes a plain ``bool`` rather than ``Settings`` so this module keeps its
    import-light property (see the module docstring).
    """
    if semantic_enabled:
        return min(config.index_concurrency, 2)
    return config.index_concurrency


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
