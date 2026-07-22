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
    SemanticOverrides,
    effective_workers,
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
def test_branches_defaults_to_empty() -> None:
    """Empty (the default) means default-branch-only -- old configs are unaffected."""
    cfg = parse_config(_MINIMAL, source="cfg")
    assert cfg.connections[0].branches == []


@pytest.mark.unit
def test_branches_accepts_glob_patterns() -> None:
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"    branches: ['release/*', 'staging']\n"
    )
    cfg = parse_config(raw, source="cfg")
    assert cfg.connections[0].branches == ["release/*", "staging"]


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


# --- index_concurrency ------------------------------------------------------


@pytest.mark.unit
def test_index_concurrency_defaults_to_four() -> None:
    """Omitting the field is the supported shape -- v1 configs predate it."""
    assert parse_config(_MINIMAL, source="cfg").index_concurrency == 4


@pytest.mark.unit
@pytest.mark.parametrize("value", [1, 4, 8])
def test_index_concurrency_accepts_in_range(value: int) -> None:
    raw = b"version: 1\nconnections:\n  - type: github\n    users: [u]\nindex_concurrency: %d\n" % (
        value
    )

    assert parse_config(raw, source="cfg").index_concurrency == value


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, 9])
def test_index_concurrency_out_of_range_raises_config_error(value: int) -> None:
    """Both bounds are enforced, and the failure reaches callers as ConfigError."""
    raw = b"version: 1\nconnections:\n  - type: github\n    users: [u]\nindex_concurrency: %d\n" % (
        value
    )

    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="cfg")

    assert "index_concurrency" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("configured", "semantic_enabled", "expected"),
    [
        (8, False, 8),
        (8, True, 2),
        (4, True, 2),
        (1, True, 1),  # the clamp is a ceiling, never a floor
        (1, False, 1),
    ],
)
def test_effective_workers(configured: int, semantic_enabled: bool, expected: int) -> None:
    cfg = RepoConfig.model_validate(
        {
            "version": 1,
            "connections": [{"type": "github", "users": ["u"]}],
            "index_concurrency": configured,
        }
    )

    assert effective_workers(cfg, semantic_enabled=semantic_enabled) == expected


# --- semantic_max_chunks_per_repo (per-repo chunk-cap override) ------------


@pytest.mark.unit
def test_semantic_max_chunks_per_repo_defaults_to_empty() -> None:
    """Omitting the field is the supported shape -- every config predating this
    override is unaffected: no repo gets one."""
    assert parse_config(_MINIMAL, source="cfg").semantic_max_chunks_per_repo == {}


@pytest.mark.unit
def test_semantic_max_chunks_per_repo_canonicalises_keys() -> None:
    """A URL-spelled key must land under the same ``org/repo`` resolve_repos uses,
    or the override would silently never match at indexing time."""
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic_max_chunks_per_repo:\n"
        b'  "https://github.com/acme/huge-monorepo.git": 20000\n'
    )
    cfg = parse_config(raw, source="cfg")
    assert cfg.semantic_max_chunks_per_repo == {"acme/huge-monorepo": 20000}


@pytest.mark.unit
@pytest.mark.parametrize("value", [0, -1])
def test_semantic_max_chunks_per_repo_rejects_non_positive_values(value: int) -> None:
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic_max_chunks_per_repo:\n  acme/widgets: %d\n" % value
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="cfg")
    assert "semantic_max_chunks_per_repo" in str(excinfo.value)


@pytest.mark.unit
def test_semantic_max_chunks_per_repo_rejects_non_int_value() -> None:
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic_max_chunks_per_repo:\n  acme/widgets: not-a-number\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="cfg")
    assert "semantic_max_chunks_per_repo" in str(excinfo.value)


@pytest.mark.unit
def test_semantic_max_chunks_per_repo_rejects_unparseable_repo_key() -> None:
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic_max_chunks_per_repo:\n"
        b'  "https://gitlab.com/acme/widgets": 20000\n'
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="cfg")
    assert "unsupported host" in str(excinfo.value)


@pytest.mark.unit
def test_semantic_max_chunks_per_repo_rejects_duplicate_keys_post_casefold() -> None:
    """``Acme/Widgets`` and ``acme/widgets`` are the same GitHub repo -- YAML itself
    treats them as distinct mapping keys, so the collision must be caught here."""
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic_max_chunks_per_repo:\n"
        b"  Acme/Widgets: 10000\n"
        b"  acme/widgets: 20000\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="cfg")
    assert "duplicate" in str(excinfo.value)


# --- semantic: block (config.yaml as the job's semantic-config surface) -----


@pytest.mark.unit
def test_semantic_block_absent_is_a_noop() -> None:
    """An unmodified config gets the default empty overlay: every field None, and
    ``settings_overrides()`` empty -- so ``indexer.job`` never model_copies cfg."""
    cfg = parse_config(_MINIMAL, source="cfg")
    assert cfg.semantic == SemanticOverrides()
    assert cfg.semantic.settings_overrides() == {}


@pytest.mark.unit
def test_semantic_block_full_maps_every_field_to_its_settings_name() -> None:
    """A fully-populated block emits all six Settings-named keys, values intact."""
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic:\n"
        b"  enabled: false\n"
        b"  max_chunks_per_repo: 12000\n"
        b"  embedding_endpoint: /custom/embeddings\n"
        b"  embedding_model: acme.embed-v2\n"
        b"  embedding_batch_size: 32\n"
        b"  embedding_timeout_s: 15.0\n"
    )
    cfg = parse_config(raw, source="cfg")
    assert cfg.semantic.settings_overrides() == {
        "semantic_enabled": False,
        "semantic_max_chunks_per_repo": 12000,
        "semantic_embedding_endpoint": "/custom/embeddings",
        "semantic_embedding_model": "acme.embed-v2",
        "semantic_embedding_batch_size": 32,
        "semantic_embedding_timeout_s": 15.0,
    }


@pytest.mark.unit
def test_semantic_block_partial_emits_only_set_fields() -> None:
    """Unset fields stay out of the overlay so they fall through to env/default --
    emitting them as None would clobber the value the operator meant to inherit."""
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic:\n  max_chunks_per_repo: 20000\n"
    )
    cfg = parse_config(raw, source="cfg")
    assert cfg.semantic.settings_overrides() == {"semantic_max_chunks_per_repo": 20000}
    # enabled:false is a real override; enabled unset must NOT surface as False.
    assert cfg.semantic.enabled is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_chunks_per_repo", b"0"),
        ("max_chunks_per_repo", b"-1"),
        ("embedding_batch_size", b"0"),
        ("embedding_batch_size", b"-5"),
        ("embedding_timeout_s", b"0"),
        ("embedding_timeout_s", b"-2.5"),
        ("embedding_endpoint", b'""'),
        ("embedding_model", b'""'),
    ],
)
def test_semantic_block_rejects_out_of_bound_values(field: str, value: bytes) -> None:
    """Bounds live on the fields (ge/gt/min_length), so a bad value fails at parse
    time and reaches the job as ConfigError naming both the block and the field."""
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic:\n  " + field.encode() + b": " + value + b"\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="cfg")
    message = str(excinfo.value)
    assert "semantic" in message
    assert field in message


@pytest.mark.unit
def test_bad_semantic_block_surfaces_as_config_error_through_parse_config() -> None:
    """AC 4: a misvalidated ``semantic:`` block fails the run at parse time with the
    ConfigError contract (path in the message), exactly like index_concurrency."""
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic:\n  max_chunks_per_repo: 0\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="/Workspace/x/config.yaml")
    message = str(excinfo.value)
    assert "/Workspace/x/config.yaml" in message
    assert "max_chunks_per_repo" in message


@pytest.mark.unit
def test_semantic_block_rejects_unknown_key() -> None:
    """A typo'd key must fail loud, not silently leave semantic ON.

    ``extra="forbid"`` on SemanticOverrides makes ``enable: false`` (a typo for
    ``enabled: false``) a parse error rather than an ignored no-op -- fail-closed
    on the job's only semantic kill switch."""
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\nsemantic:\n  enable: false\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="cfg")
    message = str(excinfo.value)
    assert "semantic" in message
    assert "enable" in message


@pytest.mark.unit
@pytest.mark.parametrize("value", ["@evil.com/x", "https://evil/x", "//evil/x"])
def test_semantic_embedding_endpoint_rejects_non_workspace_relative_paths(value: str) -> None:
    """A non-workspace-relative endpoint could exfiltrate the workspace token.

    ``app.embed`` concatenates ``f"{host}{path}"``, so ``@evil.com/x`` parses as
    userinfo@host and an absolute/protocol-relative URL leaves the workspace. The
    config surface rejects all three at parse time (through ConfigError)."""
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic:\n  embedding_endpoint: '" + value.encode() + b"'\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        parse_config(raw, source="cfg")
    assert "embedding_endpoint" in str(excinfo.value)


@pytest.mark.unit
def test_semantic_embedding_endpoint_accepts_the_workspace_relative_default() -> None:
    """The shipped default route path is a valid workspace-relative endpoint."""
    raw = (
        b"version: 1\nconnections:\n  - type: github\n    users: [u]\n"
        b"semantic:\n  embedding_endpoint: /ai-gateway/mlflow/v1/embeddings\n"
    )
    cfg = parse_config(raw, source="cfg")
    assert cfg.semantic.settings_overrides() == {
        "semantic_embedding_endpoint": "/ai-gateway/mlflow/v1/embeddings"
    }


@pytest.mark.unit
def test_settings_overrides_keys_are_real_settings_fields_and_types_survive() -> None:
    """Type-aware tripwire: the string-literal keys in ``settings_overrides`` must
    stay pinned to real ``app.config.Settings`` fields, and their VALUES must still
    satisfy those fields' types/constraints.

    ``repo_config`` cannot import ``app.config`` (import-light contract), so the
    literals could silently drift from the real field names or from their types.
    This test -- the only place in this module that touches ``Settings`` -- catches
    both: a name-drift check against ``Settings.model_fields``, then a re-validation
    of the overlaid model, because ``model_copy(update=...)`` skips validation and
    would otherwise let a wrong-typed value through unnoticed.
    """
    from app.config import Settings

    ov = SemanticOverrides(
        enabled=False,
        max_chunks_per_repo=1234,
        embedding_endpoint="/custom/embeddings",
        embedding_model="custom.model",
        embedding_batch_size=8,
        embedding_timeout_s=5.5,
    )
    overrides = ov.settings_overrides()
    # All six set -> all six emitted, and every key is a real Settings field.
    assert len(overrides) == 6
    assert set(overrides) <= set(Settings.model_fields)

    # model_copy(update=) does NOT validate; re-validating the dumped model is what
    # turns a future type/constraint drift into a loud failure here.
    overlaid = Settings().model_copy(update=overrides)
    revalidated = Settings.model_validate(overlaid.model_dump())
    assert revalidated.semantic_enabled is False
    assert revalidated.semantic_max_chunks_per_repo == 1234
    assert revalidated.semantic_embedding_endpoint == "/custom/embeddings"
    assert revalidated.semantic_embedding_model == "custom.model"
    assert revalidated.semantic_embedding_batch_size == 8
    assert revalidated.semantic_embedding_timeout_s == 5.5


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
def test_shipped_config_is_a_template_that_fails_fast() -> None:
    """The shipped config.yaml is a fork-me template with every selector commented
    out. Deployed unedited it must refuse to load — never silently index someone
    else's repos. Located relative to this file, so invocation directory is
    irrelevant."""
    with pytest.raises(ConfigError, match="connection selects nothing"):
        parse_config(_SHIPPED_CONFIG.read_bytes(), source=str(_SHIPPED_CONFIG))


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
