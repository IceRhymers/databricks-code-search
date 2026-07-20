"""Unit tests for the least-privilege grant builders.

Hermetic (no DB): these assert both the presence of intended privileges and,
crucially, the *absence* of privileges each role must never receive, plus that
hostile identifiers are rejected before any SQL is produced.
"""

from __future__ import annotations

import pytest

from app.db.grants import (
    build_app_grants,
    build_job_grants,
    quote_ident,
    validate_role,
)

SCHEMA = "code_search"
APP_ROLE = "app_ro"
JOB_ROLE = "indexer_rw"


# --- app (read-only) role ---------------------------------------------------


@pytest.mark.unit
def test_app_grants_contains_readonly_privileges() -> None:
    stmts = build_app_grants(SCHEMA, APP_ROLE)
    joined = "\n".join(stmts)
    assert "GRANT USAGE ON SCHEMA" in joined
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA" in joined
    assert "ALTER DEFAULT PRIVILEGES" in joined and "GRANT SELECT ON TABLES" in joined


@pytest.mark.unit
def test_app_grants_has_no_write_or_ddl() -> None:
    joined = "\n".join(build_app_grants(SCHEMA, APP_ROLE)).upper()
    for forbidden in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER TABLE", "OWNER", "DROP"):
        assert forbidden not in joined, f"app role must never receive {forbidden}"


@pytest.mark.unit
def test_app_grants_reference_both_idents_quoted() -> None:
    stmts = build_app_grants(SCHEMA, APP_ROLE)
    joined = "\n".join(stmts)
    assert f'"{SCHEMA}"' in joined
    assert f'"{APP_ROLE}"' in joined


# --- job (read-write, no DDL) role ------------------------------------------


@pytest.mark.unit
def test_job_grants_contains_write_privileges() -> None:
    joined = "\n".join(build_job_grants(SCHEMA, JOB_ROLE))
    assert "GRANT USAGE ON SCHEMA" in joined
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA" in joined
    assert "GRANT USAGE ON ALL SEQUENCES IN SCHEMA" in joined
    assert "ALTER DEFAULT PRIVILEGES" in joined
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES" in joined
    assert "GRANT USAGE ON SEQUENCES" in joined


@pytest.mark.unit
def test_job_grants_include_select_on_existing_and_future_tables() -> None:
    """The job reads before it writes.

    ``indexer.job._read_stamps`` issues a plain ``SELECT`` on ``repos`` before
    fan-out, so a job role without ``SELECT`` fails the whole run at its first
    query. Both halves are asserted: the immediate grant covers tables that
    exist at grant time, and ``ALTER DEFAULT PRIVILEGES`` covers tables a later
    migration creates -- without it a new table silently reintroduces the gap.
    """
    stmts = build_job_grants(SCHEMA, JOB_ROLE)
    assert any(
        s.startswith("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA") for s in stmts
    ), "job role must receive SELECT on the tables that exist now"
    assert any(
        "ALTER DEFAULT PRIVILEGES" in s and "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES" in s
        for s in stmts
    ), "job role must receive SELECT on tables created by future migrations"


@pytest.mark.unit
def test_job_grants_has_no_ddl() -> None:
    """The forbidden token is ``ALTER TABLE``, not bare ``ALTER``.

    ``ALTER DEFAULT PRIVILEGES`` is a grant-management statement, not DDL on a
    table, so it is deliberately permitted by this list.
    """
    joined = "\n".join(build_job_grants(SCHEMA, JOB_ROLE)).upper()
    for forbidden in ("CREATE", "ALTER TABLE", "OWNER", "DROP", "TRUNCATE"):
        assert forbidden not in joined, f"job role must never receive {forbidden}"


@pytest.mark.unit
def test_job_grants_reference_both_idents_quoted() -> None:
    joined = "\n".join(build_job_grants(SCHEMA, JOB_ROLE))
    assert f'"{SCHEMA}"' in joined
    assert f'"{JOB_ROLE}"' in joined


# --- identifier validation + quoting ----------------------------------------


@pytest.mark.unit
def test_validate_role_returns_name_unchanged() -> None:
    assert validate_role("app_ro") == "app_ro"
    assert validate_role("A-Za-z0-9_-") == "A-Za-z0-9_-"
    assert validate_role("x" * 63) == "x" * 63


@pytest.mark.unit
def test_quote_ident_produces_double_quoted_token() -> None:
    quoted = quote_ident("app_ro")
    assert quoted == '"app_ro"'
    assert quoted.startswith('"') and quoted.endswith('"')


@pytest.mark.unit
@pytest.mark.parametrize(
    "hostile",
    ['a"b', "x; DROP TABLE t", "has space", "", "x" * 64, "café", "rôle"],
)
def test_validate_role_rejects_hostile_identifiers(hostile: str) -> None:
    with pytest.raises(ValueError):
        validate_role(hostile)


@pytest.mark.unit
@pytest.mark.parametrize(
    "hostile",
    ['a"b', "x; DROP TABLE t", "has space", "", "x" * 64, "café", "rôle"],
)
def test_quote_ident_rejects_hostile_identifiers(hostile: str) -> None:
    with pytest.raises(ValueError):
        quote_ident(hostile)


@pytest.mark.unit
def test_builders_reject_hostile_schema_and_role() -> None:
    with pytest.raises(ValueError):
        build_app_grants('bad"schema', APP_ROLE)
    with pytest.raises(ValueError):
        build_app_grants(SCHEMA, "x; DROP TABLE t")
    with pytest.raises(ValueError):
        build_job_grants('bad"schema', JOB_ROLE)
    with pytest.raises(ValueError):
        build_job_grants(SCHEMA, "has space")


@pytest.mark.unit
def test_no_hostile_input_yields_unescaped_injection() -> None:
    # Every statement from a valid build wraps identifiers in balanced double
    # quotes and contains no stray semicolons that could split statements.
    for stmts in (
        build_app_grants(SCHEMA, APP_ROLE),
        build_job_grants(SCHEMA, JOB_ROLE),
    ):
        for stmt in stmts:
            assert stmt.count('"') % 2 == 0, f"unbalanced quotes: {stmt!r}"
            assert ";" not in stmt, f"unexpected statement separator: {stmt!r}"
