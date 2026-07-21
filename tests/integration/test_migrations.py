"""Integration tests for the core migration chain against a Lakebase branch.

Each test runs the migration in its own throwaway schema via an *injected*
connection (the same path ``scripts/migrate.py`` uses), with the Alembic version
table pinned to that schema. Requires a Lakebase branch whose project preloads
``lakebase_vector,lakebase_text`` (``upgrade head`` includes the 0004 semantic
revision; see docs/runbooks/ci-lakebase.md).

The enforcement test exercises the least-privilege grants on the *same* superuser
connection via ``SET ROLE`` to a NOLOGIN role; opening a second engine would
connect as the CI superuser and silently bypass the grants under test.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import psycopg
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.exc import ProgrammingError

from app.db.client import create_db_engine
from app.db.grants import build_app_grants, build_job_grants
from app.db.models import Base
from indexer.hashing import content_sha

_MIGRATE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "migrate.py"


class Migrated(NamedTuple):
    conn: Connection
    schema: str
    config: Config


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _load_migrate() -> ModuleType:
    """Load scripts/migrate.py by path (scripts/ is not an importable package)."""
    spec = importlib.util.spec_from_file_location("migrate_under_test", _MIGRATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def migrated() -> Iterator[Migrated]:
    """Create a throwaway schema, run ``upgrade head`` into it, yield the handle."""
    schema = _unique("test_migr")
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.commit()

        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        config.attributes["version_table_schema"] = schema

        command.upgrade(config, "head")
        conn.commit()

        yield Migrated(conn=conn, schema=schema, config=config)
    finally:
        conn.rollback()
        conn.execute(text("RESET ROLE"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_schema_fidelity(migrated: Migrated) -> None:
    conn, schema = migrated.conn, migrated.schema

    tables = (
        conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    assert {"repos", "files", "symbols", "repo_branches", "chunks"} <= set(tables)

    rows = conn.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s"),
        {"s": schema},
    ).all()
    by_name = {name: definition for name, definition in rows}
    for idx in ("ix_files_content_trgm", "ix_files_path_trgm", "ix_symbols_name_trgm"):
        assert idx in by_name, f"missing index {idx}"
        assert "USING gin" in by_name[idx]
        assert "gin_trgm_ops" in by_name[idx]
    assert "ix_files_branches_gin" in by_name
    assert "USING gin" in by_name["ix_files_branches_gin"]

    fk_count = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            "  AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' "
            "  AND tc.table_schema = :s AND tc.table_name = 'files' "
            "  AND kcu.column_name = 'repo_id'"
        ),
        {"s": schema},
    ).scalar()
    assert fk_count and fk_count >= 1

    # Post-0003: the path-uniqueness key is (repo_id, path, content_sha), not
    # (repo_id, path) -- multi-branch content dedup allows one path to have
    # multiple content versions.
    unique_cols = (
        conn.execute(
            text(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'UNIQUE' "
                "  AND tc.table_schema = :s AND tc.table_name = 'files' "
                "  AND tc.constraint_name = 'uq_files_repo_path_sha'"
            ),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    assert {"repo_id", "path", "content_sha"} <= set(unique_cols)


@pytest.mark.integration
@pytest.mark.xfail(
    strict=False,
    reason="Alembic 1.18.5 does not compare GIN opclasses, so the trgm indexes "
    "re-appear as spurious diffs; documents the limitation, not a real drift.",
)
def test_autogenerate_reports_no_diff(migrated: Migrated) -> None:
    conn = migrated.conn
    context = MigrationContext.configure(
        connection=conn,
        opts={"target_metadata": Base.metadata},
    )
    diffs = compare_metadata(context, Base.metadata)
    assert diffs == [], f"unexpected autogenerate diffs: {diffs}"


@pytest.mark.integration
def test_grant_enforcement_via_set_role(migrated: Migrated) -> None:
    conn, schema = migrated.conn, migrated.schema
    app_ro = _unique("app_ro")
    job_rw = _unique("job_rw")
    try:
        conn.execute(text(f"CREATE ROLE {app_ro} NOLOGIN"))
        conn.execute(text(f"CREATE ROLE {job_rw} NOLOGIN"))
        for stmt in build_app_grants(schema, app_ro):
            conn.execute(text(stmt))
        for stmt in build_job_grants(schema, job_rw):
            conn.execute(text(stmt))
        conn.commit()

        # Read-only role: SELECT works, INSERT is denied.
        conn.execute(text(f"SET ROLE {app_ro}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("SELECT * FROM files")).all()
        # SQLAlchemy wraps the psycopg error; assert the underlying cause.
        with pytest.raises(ProgrammingError) as excinfo:
            conn.execute(text("INSERT INTO repos (name) VALUES ('denied')"))
        assert isinstance(excinfo.value.orig, psycopg.errors.InsufficientPrivilege)
        # The failed statement aborts the tx; rollback also undoes the SET ROLE.
        conn.rollback()

        # Writer role: INSERT works.
        conn.execute(text(f"SET ROLE {job_rw}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("INSERT INTO repos (name) VALUES ('allowed')"))
        conn.execute(text("RESET ROLE"))
        conn.rollback()
    finally:
        conn.rollback()
        conn.execute(text("RESET ROLE"))
        for role in (app_ro, job_rw):
            conn.execute(text(f"DROP OWNED BY {role} CASCADE"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        conn.commit()


@pytest.mark.integration
def test_downgrade_drops_tables_but_keeps_extension(migrated: Migrated) -> None:
    conn, schema = migrated.conn, migrated.schema

    command.downgrade(migrated.config, "base")
    conn.commit()

    remaining = (
        conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = :s AND table_name IN ('repos', 'files', 'symbols')"
            ),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    assert remaining == []

    trgm = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")).scalar()
    assert trgm == 1, "downgrade must not drop the shared pg_trgm extension"


@pytest.mark.integration
def test_0002_backfill_window_both_branches() -> None:
    """Seed at 0001, upgrade to 0002: a recent row backfills, a stale row stays NULL.

    Both branches must be asserted or the ``48 hours`` filter is untested -- an
    unfiltered ``UPDATE`` would pass a recent-row-only assertion.
    """
    schema = _unique("test_backfill")
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.commit()

        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        config.attributes["version_table_schema"] = schema

        command.upgrade(config, "0001")
        conn.commit()

        conn.execute(
            text(
                "INSERT INTO repos (name, last_indexed_at) VALUES "
                "('recent', now()), "
                "('stale', now() - interval '10 days'), "
                "('never', NULL)"
            )
        )
        conn.commit()

        command.upgrade(config, "0002")
        conn.commit()

        versions = dict(conn.execute(text("SELECT name, index_semantics_version FROM repos")).all())
        assert versions["recent"] == 1
        assert versions["stale"] is None
        assert versions["never"] is None

        # downgrade returns repos to its exact 0001 shape.
        command.downgrade(config, "0001")
        conn.commit()
        cols = (
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'repos'"
                ),
                {"s": schema},
            )
            .scalars()
            .all()
        )
        assert "index_semantics_version" not in cols
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_semantic_chunks_created_at_head(migrated: Migrated) -> None:
    """0004 rides the core chain: `upgrade head` creates chunks + both lakebase indexes."""
    conn, schema = migrated.conn, migrated.schema

    assert conn.execute(text("SELECT to_regclass('chunks')")).scalar() is not None

    rows = conn.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = :s"),
        {"s": schema},
    ).all()
    by_name = {name: definition for name, definition in rows}
    assert "ix_chunks_embedding_ann" in by_name
    assert "lakebase_ann" in by_name["ix_chunks_embedding_ann"]
    assert "ix_chunks_ts_bm25" in by_name
    assert "lakebase_bm25" in by_name["ix_chunks_ts_bm25"]

    cols = (
        conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'chunks'"
            ),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    # Line-range columns present from birth (issue #44).
    assert {"start_line", "end_line"} <= set(cols)


@pytest.mark.integration
def test_0004_guard_preserves_preexisting_chunks() -> None:
    """AC6/idempotency: a schema that already has chunks (old gated migrate-semantic)
    upgrades to head without re-running the DDL -- data survives, the line-range
    columns are added, and the orphaned alembic_version_semantic table is dropped."""
    schema = _unique("test_0004_guard")
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.commit()

        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        config.attributes["version_table_schema"] = schema

        command.upgrade(config, "0003")
        conn.commit()

        # Simulate the retired gated migration's leftovers: a chunks table with data
        # (pre-#44 shape, no line columns) and its separate version table.
        conn.execute(text("INSERT INTO repos (name) VALUES ('r1')"))
        repo_id = conn.execute(text("SELECT id FROM repos WHERE name = 'r1'")).scalar()
        conn.execute(
            text("INSERT INTO files (repo_id, path, content) VALUES (:r, 'a.py', 'x')"),
            {"r": repo_id},
        )
        file_id = conn.execute(text("SELECT id FROM files WHERE path = 'a.py'")).scalar()
        conn.execute(
            text(
                "CREATE TABLE chunks ("
                "id bigserial PRIMARY KEY, "
                "file_id integer NOT NULL REFERENCES files(id) ON DELETE CASCADE, "
                "chunk_index integer NOT NULL, "
                "content text NOT NULL)"
            )
        )
        conn.execute(
            text("INSERT INTO chunks (file_id, chunk_index, content) VALUES (:f, 0, 'seed')"),
            {"f": file_id},
        )
        conn.execute(text("CREATE TABLE alembic_version_semantic (version_num varchar(32))"))
        conn.commit()

        command.upgrade(config, "head")
        conn.commit()

        # Data preserved (guard skipped the CREATE), line columns added, orphan dropped.
        assert conn.execute(text("SELECT count(*) FROM chunks")).scalar() == 1
        cols = (
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :s AND table_name = 'chunks'"
                ),
                {"s": schema},
            )
            .scalars()
            .all()
        )
        assert {"start_line", "end_line"} <= set(cols)
        assert conn.execute(text("SELECT to_regclass('alembic_version_semantic')")).scalar() is None
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_migrate_run_apply_grants_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive scripts/migrate.py run(apply_grants=True) — the real execution path.

    Exercises env-var resolution, the pg_roles existence check, and grant
    application against roles that actually exist, then proves enforcement via
    SET ROLE. This is the coverage the builder-only enforcement test lacks.
    """
    migrate = _load_migrate()
    schema = _unique("test_apply")
    app_role = _unique("app_sp")
    job_role = _unique("job_wr")

    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"CREATE ROLE {app_role} NOLOGIN"))
        conn.execute(text(f"CREATE ROLE {job_role} NOLOGIN"))
        conn.commit()

        monkeypatch.setenv("PGSCHEMA", schema)
        monkeypatch.setenv("APP_SP_ROLE", app_role)
        monkeypatch.setenv("JOB_WRITER_ROLE", job_role)

        # Opens its own engine/connection, runs upgrade + grants, and commits.
        migrate.run(apply_grants=True)

        # DDL landed in the grant target schema (fix for schema divergence).
        tables = (
            conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
                {"s": schema},
            )
            .scalars()
            .all()
        )
        assert {"repos", "files", "symbols"} <= set(tables)

        # Enforcement: app role reads but cannot write; job role can write.
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text(f"SET ROLE {app_role}"))
        conn.execute(text("SELECT * FROM files")).all()
        with pytest.raises(ProgrammingError) as excinfo:
            conn.execute(text("INSERT INTO repos (name) VALUES ('denied')"))
        assert isinstance(excinfo.value.orig, psycopg.errors.InsufficientPrivilege)
        conn.rollback()

        conn.execute(text(f"SET ROLE {job_role}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("INSERT INTO repos (name) VALUES ('allowed')"))
        conn.execute(text("RESET ROLE"))
        conn.rollback()
    finally:
        conn.rollback()
        conn.execute(text("RESET ROLE"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        for role in (app_role, job_role):
            conn.execute(text(f"DROP OWNED BY {role} CASCADE"))
            conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_migrate_run_apply_grants_app_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """With only APP_SP_ROLE set, the app read-only grant lands and no job grant is applied.

    Proves the independent-grant path (Decision C1): the app role can SELECT but its INSERT
    raises InsufficientPrivilege, confirming it never received the write grants.
    """
    migrate = _load_migrate()
    schema = _unique("test_app_only")
    app_role = _unique("app_sp")

    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"CREATE ROLE {app_role} NOLOGIN"))
        conn.commit()

        monkeypatch.setenv("PGSCHEMA", schema)
        monkeypatch.setenv("APP_SP_ROLE", app_role)
        monkeypatch.delenv("JOB_WRITER_ROLE", raising=False)

        migrate.run(apply_grants=True)

        # App role reads but cannot write (no job grant was applied to it).
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text(f"SET ROLE {app_role}"))
        conn.execute(text("SELECT * FROM files")).all()
        with pytest.raises(ProgrammingError) as excinfo:
            conn.execute(text("INSERT INTO repos (name) VALUES ('denied')"))
        assert isinstance(excinfo.value.orig, psycopg.errors.InsufficientPrivilege)
        conn.rollback()
    finally:
        conn.rollback()
        conn.execute(text("RESET ROLE"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"DROP OWNED BY {app_role} CASCADE"))
        conn.execute(text(f"DROP ROLE IF EXISTS {app_role}"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_migrate_apply_grants_neither_role_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """--apply-grants with neither role set raises before touching privileges."""
    migrate = _load_migrate()
    schema = _unique("test_neither")

    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.commit()

        monkeypatch.setenv("PGSCHEMA", schema)
        monkeypatch.delenv("APP_SP_ROLE", raising=False)
        monkeypatch.delenv("JOB_WRITER_ROLE", raising=False)

        with pytest.raises(RuntimeError, match="at least one"):
            migrate.run(apply_grants=True)
    finally:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_migrate_apply_grants_missing_role_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A grant target that is not in pg_roles hard-fails (no silent skip, no self-CREATE)."""
    migrate = _load_migrate()
    schema = _unique("test_missing")

    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.commit()

        monkeypatch.setenv("PGSCHEMA", schema)
        monkeypatch.setenv("APP_SP_ROLE", _unique("absent_app"))
        monkeypatch.setenv("JOB_WRITER_ROLE", _unique("absent_job"))

        with pytest.raises(RuntimeError, match="does not exist"):
            migrate.run(apply_grants=True)
    finally:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_0003_backfill_and_shape() -> None:
    """Seed at 0002 (pre-multi-branch), upgrade to 0003: backfill matches content_sha().

    Covers a repo with a ``default_branch`` and one with ``NULL`` (exercising the
    ``coalesce(default_branch,'HEAD')`` path), a NULL-content file, and multibyte
    UTF-8 + trailing-newline content -- the same case set the Phase-0 parity gate
    covers, now proven through the actual migration SQL rather than a bare query.
    """
    schema = _unique("test_0003_backfill")
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.commit()

        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        config.attributes["version_table_schema"] = schema

        command.upgrade(config, "0002")
        conn.commit()

        conn.execute(
            text(
                "INSERT INTO repos (name, default_branch, last_indexed_commit, "
                "last_indexed_at, index_semantics_version) VALUES "
                "('with-default', 'main', 'deadbeef', now(), 1), "
                "('no-default', NULL, NULL, NULL, NULL)"
            )
        )
        conn.commit()
        repo_ids = dict(conn.execute(text("SELECT name, id FROM repos")).all())

        conn.execute(
            text(
                "INSERT INTO files (repo_id, path, content, commit) VALUES "
                "(:r1, 'a.py', 'héllo→λ', 'deadbeef'), "
                "(:r1, 'b.py', NULL, 'deadbeef'), "
                "(:r2, 'c.py', 'trailing' || chr(10), NULL)"
            ),
            {"r1": repo_ids["with-default"], "r2": repo_ids["no-default"]},
        )
        conn.commit()

        command.upgrade(config, "0003")
        conn.commit()

        rows = conn.execute(
            text("SELECT path, content, content_sha, branches FROM files ORDER BY path")
        ).all()
        by_path = {r.path: r for r in rows}

        assert by_path["a.py"].content_sha == content_sha("héllo→λ")
        assert by_path["a.py"].branches == ["main"]
        assert by_path["b.py"].content_sha == content_sha(None)
        assert by_path["b.py"].branches == ["main"]
        assert by_path["c.py"].content_sha == content_sha("trailing\n")
        assert by_path["c.py"].branches == ["HEAD"]

        branch_by_repo = dict(
            conn.execute(
                text(
                    "SELECT r.name, rb.branch FROM repo_branches rb "
                    "JOIN repos r ON r.id = rb.repo_id"
                )
            ).all()
        )
        assert branch_by_repo["with-default"] == "main"
        assert branch_by_repo["no-default"] == "HEAD"

        stamp = conn.execute(
            text(
                "SELECT rb.last_indexed_commit, rb.index_semantics_version FROM repo_branches rb "
                "JOIN repos r ON r.id = rb.repo_id WHERE r.name = 'with-default'"
            )
        ).one()
        assert stamp.last_indexed_commit == "deadbeef"
        assert stamp.index_semantics_version == 1
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_0003_downgrade_clean_on_single_branch_data(migrated: Migrated) -> None:
    """A path with exactly one content version downgrades cleanly to 0002 shape."""
    conn, schema = migrated.conn, migrated.schema

    conn.execute(text("INSERT INTO repos (name, default_branch) VALUES ('r1', 'main')"))
    conn.commit()
    repo_id = conn.execute(text("SELECT id FROM repos WHERE name = 'r1'")).scalar()
    conn.execute(
        text(
            "INSERT INTO files (repo_id, path, content, commit, content_sha, branches) VALUES "
            "(:r, 'a.py', 'x', 'sha1', :sha, ARRAY['main'])"
        ),
        {"r": repo_id, "sha": content_sha("x")},
    )
    conn.commit()

    command.downgrade(migrated.config, "0002")
    conn.commit()

    cols = (
        conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = 'files'"
            ),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    assert "content_sha" not in cols
    assert "branches" not in cols

    tables = (
        conn.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    assert "repo_branches" not in tables

    unique_cols = (
        conn.execute(
            text(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                "  AND tc.table_schema = kcu.table_schema "
                "WHERE tc.constraint_type = 'UNIQUE' "
                "  AND tc.table_schema = :s AND tc.table_name = 'files' "
                "  AND tc.constraint_name = 'uq_files_repo_id_path'"
            ),
            {"s": schema},
        )
        .scalars()
        .all()
    )
    assert {"repo_id", "path"} <= set(unique_cols)


@pytest.mark.integration
def test_0003_downgrade_blocked_by_multi_branch_data(migrated: Migrated) -> None:
    """Two content versions of one path (real multi-branch divergence) block downgrade."""
    conn = migrated.conn

    conn.execute(text("INSERT INTO repos (name, default_branch) VALUES ('r1', 'main')"))
    conn.commit()
    repo_id = conn.execute(text("SELECT id FROM repos WHERE name = 'r1'")).scalar()
    conn.execute(
        text(
            "INSERT INTO files (repo_id, path, content, commit, content_sha, branches) VALUES "
            "(:r, 'a.py', 'x', 'sha1', :sha1, ARRAY['main']), "
            "(:r, 'a.py', 'y', 'sha2', :sha2, ARRAY['feature'])"
        ),
        {"r": repo_id, "sha1": content_sha("x"), "sha2": content_sha("y")},
    )
    conn.commit()

    with pytest.raises(RuntimeError, match="multi-branch data present"):
        command.downgrade(migrated.config, "0002")
    conn.rollback()
