"""Unit tests for indexer.repo_config: schema, parsing, canonicalisation, read seam.

This module must NOT import ``indexer.job`` -- that pulls ~543 modules (sqlalchemy,
tree_sitter, pydantic_settings) and the import-light property of ``repo_config`` is
what keeps these tests fast and dependency-free. The ``config_loader`` default
assertion (AC 34) lives in ``test_job.py`` for exactly this reason.

The workspace read seam is faked with ``io.BytesIO`` -- no filesystem, no creds.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from indexer.repo_config import (
    ConfigError,
    ExcludeRules,
    GitHubConnection,
    RepoConfig,
    load_config,
    normalize_repo,
    parse_config,
)

_MINIMAL = b"version: 1\nconnections:\n  - type: github\n    users: [IceRhymers]\n"


# --- normalize_repo (AC 10; relocated from test_job.py per Decision 0) -------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("acme/widgets", "acme/widgets"),
        ("https://github.com/acme/widgets", "acme/widgets"),
        ("https://github.com/acme/widgets.git", "acme/widgets"),
        ("git@github.com:acme/widgets.git", "acme/widgets"),
        ("  acme/widgets  ", "acme/widgets"),
        ("https://github.com/acme/widgets/", "acme/widgets"),
    ],
)
def test_normalize_repo_accepts(entry: str, expected: str) -> None:
    assert normalize_repo(entry) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry",
    [
        "",
        "   ",
        "acme",
        "acme/widgets/extra",
        "https://gitlab.com/acme/widgets",
        "git@bitbucket.org:acme/widgets.git",
        "https://evil.com/a/b",
        "../..",
        "acme/..",
        "../widgets",
        "https://github.com/acme/../secrets",
    ],
)
def test_normalize_repo_rejects(entry: str) -> None:
    with pytest.raises(ValueError):
        normalize_repo(entry)


# --- schema (AC 1-6) --------------------------------------------------------


@pytest.mark.unit
def test_parse_config_minimal() -> None:
    """AC 1."""
    cfg = parse_config(_MINIMAL, source="/Workspace/x/config.yaml")

    assert cfg.version == 1
    (conn,) = cfg.connections
    assert isinstance(conn, GitHubConnection)
    assert conn.type == "github"
    assert conn.users == ["IceRhymers"]


@pytest.mark.unit
def test_unknown_connection_type_rejected() -> None:
    """AC 2 -- the discriminator names both the location and the bad tag."""
    with pytest.raises(ValidationError) as excinfo:
        RepoConfig.model_validate(
            {"version": 1, "connections": [{"type": "gitlab", "orgs": ["acme"]}]}
        )

    message = str(excinfo.value)
    assert "connections.0" in message
    assert "gitlab" in message


@pytest.mark.unit
def test_missing_version_rejected() -> None:
    """AC 3a."""
    with pytest.raises(ValidationError) as excinfo:
        RepoConfig.model_validate({"connections": [{"type": "github", "users": ["u"]}]})

    assert "version" in str(excinfo.value)


@pytest.mark.unit
def test_unsupported_version_rejected() -> None:
    """AC 3b -- names both the supplied and the supported version."""
    with pytest.raises(ValidationError) as excinfo:
        RepoConfig.model_validate(
            {"version": 2, "connections": [{"type": "github", "users": ["u"]}]}
        )

    # Assert the real phrases, not bare digits: "1"/"2" appear in enough unrelated
    # pydantic messages (and in the docs URL) that a substring check on them would
    # pass even if the version literal stopped being validated at all.
    message = str(excinfo.value)
    assert "Input should be 1" in message
    assert "input_value=2" in message


@pytest.mark.unit
def test_empty_connections_rejected() -> None:
    """AC 4 -- min_length=1."""
    with pytest.raises(ValidationError) as excinfo:
        RepoConfig.model_validate({"version": 1, "connections": []})

    assert "connections" in str(excinfo.value)


@pytest.mark.unit
def test_connection_with_no_selectors_rejected() -> None:
    """AC 5 -- the validator's message plus pydantic's own error location."""
    with pytest.raises(ValidationError) as excinfo:
        RepoConfig.model_validate({"version": 1, "connections": [{"type": "github"}]})

    message = str(excinfo.value)
    assert "connections.0" in message
    assert "at least one of orgs, users, repos" in message


@pytest.mark.unit
def test_exclude_defaults() -> None:
    """AC 6."""
    cfg = parse_config(_MINIMAL, source="cfg")

    assert cfg.connections[0].exclude == ExcludeRules(
        forks=True, archived=True, repos=[], size_mb=None
    )


@pytest.mark.unit
def test_schema_error_surfaces_as_config_error_through_parse_config() -> None:
    """parse_config wraps ValidationError so callers only handle ConfigError."""
    with pytest.raises(ConfigError) as excinfo:
        parse_config(b"version: 2\nconnections:\n  - type: github\n    users: [u]\n", source="cfg")

    message = str(excinfo.value)
    assert "cfg" in message
    assert "version" in message


# --- parse failures (AC 7-8) ------------------------------------------------


@pytest.mark.unit
def test_malformed_yaml_raises_config_error() -> None:
    """AC 7 -- a raw yaml.YAMLError must not escape."""
    with pytest.raises(ConfigError) as excinfo:
        parse_config(b"version: 1\nconnections: [oops\n", source="/Workspace/x/config.yaml")

    assert "/Workspace/x/config.yaml" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.parametrize("raw", [b"- one\n- two\n", b"just a scalar\n", b"\n"])
def test_non_mapping_document_raises_config_error(raw: bytes) -> None:
    """AC 8 -- checked before pydantic sees the document."""
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="/Workspace/x/config.yaml")

    message = str(excinfo.value)
    assert "/Workspace/x/config.yaml" in message
    assert "mapping" in message or "empty" in message


# --- the shipped config (AC 9) ----------------------------------------------

_SHIPPED_CONFIG = Path(__file__).parents[2] / "config.yaml"


@pytest.mark.unit
def test_shipped_config_parses() -> None:
    """AC 9 -- located relative to this file, so invocation directory is irrelevant."""
    cfg = parse_config(_SHIPPED_CONFIG.read_bytes(), source=str(_SHIPPED_CONFIG))

    assert cfg.version == 1
    assert cfg.connections


# --- read seam (AC 11-13) ---------------------------------------------------


class _FakeWorkspace:
    def __init__(self, *, payload: bytes | None = None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls: list[str] = []

    def download(self, path: str) -> Any:
        self.calls.append(path)
        if self._error is not None:
            raise self._error
        assert self._payload is not None
        return io.BytesIO(self._payload)


class _FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        self.workspace = _FakeWorkspace(**kwargs)


@pytest.mark.unit
def test_load_config_downloads_exactly_the_given_path() -> None:
    """AC 11."""
    client = _FakeClient(payload=_MINIMAL)

    cfg = load_config(client, "/Workspace/x/config.yaml")

    assert client.workspace.calls == ["/Workspace/x/config.yaml"]
    assert cfg.connections[0].users == ["IceRhymers"]


@pytest.mark.unit
def test_not_found_becomes_config_error_with_404() -> None:
    """AC 12 -- 'never synced' is self-identifying; the SDK error does not escape."""
    from databricks.sdk.errors import NotFound

    client = _FakeClient(error=NotFound("nope"))

    with pytest.raises(ConfigError) as excinfo:
        load_config(client, "/Workspace/x/config.yaml")

    message = str(excinfo.value)
    assert "/Workspace/x/config.yaml" in message
    assert "404" in message


@pytest.mark.unit
def test_permission_denied_becomes_config_error_with_403() -> None:
    """AC 13 -- 'no read permission' is distinguishable from AC 12's 'never synced'."""
    from databricks.sdk.errors import PermissionDenied

    client = _FakeClient(error=PermissionDenied("denied"))

    with pytest.raises(ConfigError) as excinfo:
        load_config(client, "/Workspace/x/config.yaml")

    message = str(excinfo.value)
    assert "/Workspace/x/config.yaml" in message
    assert "403" in message
    assert "404" not in message


@pytest.mark.unit
def test_unexpected_sdk_error_is_still_wrapped() -> None:
    """The wrap is unconditional -- no exception type may bypass the job's handler."""
    client = _FakeClient(error=RuntimeError("transport exploded"))

    with pytest.raises(ConfigError) as excinfo:
        load_config(client, "/Workspace/x/config.yaml")

    assert "/Workspace/x/config.yaml" in str(excinfo.value)
