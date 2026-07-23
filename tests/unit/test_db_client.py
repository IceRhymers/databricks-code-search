"""Unit tests for the database engine factory and ORM models.

These are hermetic: local (PGHOST) mode must build an engine without instantiating
the Databricks SDK, and the models must expose exactly the durable-core
schema (repos / files / symbols / repo_branches) with the expected FKs, unique
constraints, and GIN indexes.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.engine import Engine

from app.db.client import _local_url, create_db_engine
from app.db.models import Base


@pytest.fixture
def local_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "localhost")
    monkeypatch.setenv("PGPORT", "5432")
    monkeypatch.setenv("PGUSER", "codesearch")
    monkeypatch.setenv("PGPASSWORD", "s3cret")
    monkeypatch.setenv("PGDATABASE", "codesearch")


@pytest.mark.unit
def test_local_url_driver_host_dbname(local_env: None) -> None:
    url = _local_url()
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "localhost"
    assert url.port == 5432
    assert url.username == "codesearch"
    assert url.database == "codesearch"


@pytest.mark.unit
def test_create_engine_local_mode_never_touches_sdk(
    local_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local (PGHOST) mode builds an engine without instantiating the Databricks SDK."""

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("Databricks SDK must not be used in local (PGHOST) mode")

    # If the local path so much as instantiates a WorkspaceClient, this fails.
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _boom)

    engine = create_db_engine()
    try:
        assert isinstance(engine, Engine)
        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.database == "codesearch"
    finally:
        engine.dispose()


@pytest.mark.unit
def test_metadata_has_exactly_durable_core_tables() -> None:
    assert set(Base.metadata.tables) == {
        "repos",
        "files",
        "symbols",
        "repo_branches",
        "reference_edges",
    }


@pytest.mark.unit
def test_repo_name_unique() -> None:
    repos = Base.metadata.tables["repos"]
    assert repos.c.name.unique is True


@pytest.mark.unit
def test_files_foreign_key_to_repos() -> None:
    files = Base.metadata.tables["files"]
    targets = {fk.column.table.name for fk in files.c.repo_id.foreign_keys}
    assert "repos" in targets
    fk = next(iter(files.c.repo_id.foreign_keys))
    assert fk.ondelete == "CASCADE"


@pytest.mark.unit
def test_files_unique_repo_path_content_sha() -> None:
    """Post-0003 the dedup key is (repo_id, path, content_sha), not (repo_id, path)."""
    files = Base.metadata.tables["files"]
    unique_cols = [
        {c.name for c in c.columns} for c in files.constraints if isinstance(c, UniqueConstraint)
    ]
    assert {"repo_id", "path", "content_sha"} in unique_cols


@pytest.mark.unit
def test_files_branches_gin_index_declared() -> None:
    files = Base.metadata.tables["files"]
    idx = next(i for i in files.indexes if i.name == "ix_files_branches_gin")
    assert idx.dialect_options["postgresql"]["using"] == "gin"
    assert {c.name for c in idx.columns} == {"branches"}


@pytest.mark.unit
def test_repo_branches_unique_repo_id_branch() -> None:
    repo_branches = Base.metadata.tables["repo_branches"]
    unique_cols = [
        {c.name for c in c.columns}
        for c in repo_branches.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert {"repo_id", "branch"} in unique_cols


@pytest.mark.unit
def test_repo_branches_foreign_key_to_repos() -> None:
    repo_branches = Base.metadata.tables["repo_branches"]
    fk = next(iter(repo_branches.c.repo_id.foreign_keys))
    assert fk.column.table.name == "repos"
    assert fk.ondelete == "CASCADE"


@pytest.mark.unit
def test_symbols_foreign_keys() -> None:
    symbols = Base.metadata.tables["symbols"]
    file_fk = next(iter(symbols.c.file_id.foreign_keys))
    assert file_fk.column.table.name == "files"
    assert file_fk.ondelete == "CASCADE"
    repo_fk = next(iter(symbols.c.repo_id.foreign_keys))
    assert repo_fk.column.table.name == "repos"


@pytest.mark.unit
def test_trgm_gin_indexes_declared() -> None:
    expected = {
        "ix_files_content_trgm": ("files", "content"),
        "ix_files_path_trgm": ("files", "path"),
        "ix_symbols_name_trgm": ("symbols", "name"),
    }
    declared: dict[str, Index] = {}
    for table in Base.metadata.tables.values():
        for idx in table.indexes:
            declared[idx.name or ""] = idx

    for name, (table_name, column) in expected.items():
        assert name in declared, f"missing index {name}"
        idx = declared[name]
        assert idx.table.name == table_name
        assert idx.dialect_options["postgresql"]["using"] == "gin"
        assert idx.dialect_options["postgresql"]["ops"] == {column: "gin_trgm_ops"}


# --- Lakebase-mode fakes (no network, no real WorkspaceClient) --------------


class _FakeCredential:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeHosts:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeStatus:
    def __init__(self, host: str) -> None:
        self.hosts = _FakeHosts(host)


class _FakeEndpoint:
    def __init__(self, host: str) -> None:
        self.status = _FakeStatus(host)


class _FakePostgres:
    def __init__(self, token: str, host: str) -> None:
        self._token = token
        self._host = host
        self.generate_calls: list[str] = []

    def generate_database_credential(self, *, endpoint: str) -> _FakeCredential:
        self.generate_calls.append(endpoint)
        return _FakeCredential(self._token)

    def get_endpoint(self, name: str) -> _FakeEndpoint:
        return _FakeEndpoint(self._host)


class _FakeMe:
    def __init__(self, user_name: str) -> None:
        self.user_name = user_name


class _FakeCurrentUser:
    def __init__(self, user_name: str) -> None:
        self._user_name = user_name

    def me(self) -> _FakeMe:
        return _FakeMe(self._user_name)


class _FakeWorkspaceClient:
    """A WorkspaceClient stand-in whose DNS host differs from the endpoint name."""

    def __init__(
        self,
        *,
        token: str = "fake-oauth-token",
        host: str = "dns-host.lakebase.example.com",
        user: str = "svc-user",
    ) -> None:
        self.postgres = _FakePostgres(token, host)
        self.current_user = _FakeCurrentUser(user)


@pytest.fixture
def lakebase_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "PGHOST",
        "LAKEBASE_ENDPOINT",
        "LAKEBASE_HOST",
        "LAKEBASE_DATABASE",
        "LAKEBASE_USER",
        "LAKEBASE_PORT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.mark.unit
def test_lakebase_engine_host_and_token_injection(
    lakebase_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeWorkspaceClient(token="tok-abc", host="dns-host.lakebase.example.com")
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", lambda: fake)

    engine = create_db_engine(endpoint="ep-name", database="db", user="u")
    try:
        # Endpoint NAME must not be conflated with the resolved DNS host.
        assert engine.url.host == "dns-host.lakebase.example.com"
        assert engine.url.host != "ep-name"

        # Fire the registered do_connect handler directly (dialect-level event).
        listeners = list(engine.dialect.dispatch.do_connect)
        assert listeners, "expected a do_connect listener to be registered"
        cparams: dict[str, object] = {}
        listeners[0](None, None, None, cparams)

        assert cparams["password"] == "tok-abc"
        assert fake.postgres.generate_calls == ["ep-name"]
    finally:
        engine.dispose()


@pytest.mark.unit
def test_lakebase_engine_pool_config(lakebase_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _FakeWorkspaceClient)

    engine = create_db_engine(endpoint="ep-name")
    try:
        assert engine.pool._recycle == 2700
        assert engine.pool._pre_ping is True
        assert engine.pool.size() == 5
    finally:
        engine.dispose()


@pytest.mark.unit
def test_lakebase_missing_credential_api_raises(
    lakebase_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _WorkspaceClientNoCred:
        def __init__(self) -> None:
            # ``postgres`` lacks generate_database_credential (older SDK shape).
            self.postgres = object()
            self.current_user = _FakeCurrentUser("svc-user")

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _WorkspaceClientNoCred)

    with pytest.raises(RuntimeError, match="0.81"):
        create_db_engine(endpoint="x")


@pytest.mark.unit
def test_lakebase_missing_endpoint_raises(
    lakebase_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _FakeWorkspaceClient)

    with pytest.raises(ValueError):
        create_db_engine()
