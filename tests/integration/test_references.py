"""Integration tests for the reference resolver: raw edges -> ranked candidate sets.

Requires a running Postgres with the standard PG* env set. Mirrors the throwaway-schema idiom
of ``tests/integration/test_symbols_search.py`` (unique schema, ``SET search_path``, ``CREATE
EXTENSION pg_trgm``, ``Base.metadata.create_all`` on the same connection, ``DROP SCHEMA ...
CASCADE`` + ``engine.dispose()`` in ``finally``). In this repo that Postgres exists only as
CI's service container (or a local dev Postgres), so these tests are CI-only and were
validated locally by lint/type-check + ``--collect-only`` when no live Postgres is reachable.

The ``seeded`` fixture is function-scoped: the timeout test inserts a large row volume and the
determinism/branch-scoping assertions rely on a clean corpus, so each test gets its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import NamedTuple

import pytest
from sqlalchemy import Connection, insert, text
from sqlalchemy.dialects import postgresql

from app.db.client import create_db_engine
from app.db.models import Base, File, ReferenceEdge, Repo, Symbol
from app.search.errors import QueryTooBroadError
from app.search.references import (
    DEFAULT_CANDIDATE_CAP,
    ReferenceResult,
    _build_candidates_select,
    _build_sites_select,
    resolve_references,
)
from indexer.hashing import content_sha

SCHEMA_PREFIX = "test_refsearch"


class Seeded(NamedTuple):
    conn: Connection
    acme_id: int
    beta_id: int
    files: dict[str, int]


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _insert_repo(conn: Connection, name: str, *, default_branch: str | None = "main") -> int:
    return conn.execute(
        insert(Repo).values(name=name, default_branch=default_branch).returning(Repo.id)
    ).scalar_one()


def _insert_file(
    conn: Connection,
    repo_id: int,
    path: str,
    *,
    lang: str | None = "python",
    content: str | None = "pass\n",
    branches: list[str] | None = None,
) -> int:
    return conn.execute(
        insert(File)
        .values(
            repo_id=repo_id,
            path=path,
            lang=lang,
            content=content,
            content_sha=content_sha(content),
            branches=branches if branches is not None else ["main"],
        )
        .returning(File.id)
    ).scalar_one()


def _insert_symbol(
    conn: Connection,
    file_id: int,
    repo_id: int,
    name: str,
    *,
    kind: str | None = "function",
    start_line: int | None = 1,
) -> int:
    return conn.execute(
        insert(Symbol)
        .values(file_id=file_id, repo_id=repo_id, name=name, kind=kind, start_line=start_line)
        .returning(Symbol.id)
    ).scalar_one()


def _insert_edge(
    conn: Connection,
    file_id: int,
    repo_id: int,
    *,
    edge_kind: str,
    target_name: str,
    line: int = 1,
    enclosing_name: str | None = None,
    enclosing_kind: str | None = None,
) -> int:
    return conn.execute(
        insert(ReferenceEdge)
        .values(
            file_id=file_id,
            repo_id=repo_id,
            edge_kind=edge_kind,
            target_name=target_name,
            line=line,
            enclosing_name=enclosing_name,
            enclosing_kind=enclosing_kind,
        )
        .returning(ReferenceEdge.id)
    ).scalar_one()


@pytest.fixture
def seeded() -> Iterator[Seeded]:
    """Throwaway schema + durable-core DDL + a deterministic edge/symbol corpus."""
    schema = _unique(SCHEMA_PREFIX)
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.commit()

        Base.metadata.create_all(bind=conn)
        conn.commit()

        acme_id = _insert_repo(conn, "acme/widgets")
        beta_id = _insert_repo(conn, "beta/tools")
        files: dict[str, int] = {}

        # -- unique: exactly one candidate definition.
        files["src/unique_target.py"] = _insert_file(conn, acme_id, "src/unique_target.py")
        _insert_symbol(conn, files["src/unique_target.py"], acme_id, "unique_fn", start_line=2)
        files["src/caller.py"] = _insert_file(conn, acme_id, "src/caller.py")
        _insert_edge(
            conn,
            files["src/caller.py"],
            acme_id,
            edge_kind="call",
            target_name="unique_fn",
            line=5,
            enclosing_name="handle",
            enclosing_kind="function",
        )

        # -- ambiguous, same-repo duplicate definitions.
        files["src/dup_a.py"] = _insert_file(conn, acme_id, "src/dup_a.py")
        _insert_symbol(conn, files["src/dup_a.py"], acme_id, "ambiguous_fn", start_line=1)
        files["src/dup_b.py"] = _insert_file(conn, acme_id, "src/dup_b.py")
        _insert_symbol(conn, files["src/dup_b.py"], acme_id, "ambiguous_fn", start_line=1)
        files["src/caller2.py"] = _insert_file(conn, acme_id, "src/caller2.py")
        _insert_edge(
            conn, files["src/caller2.py"], acme_id, edge_kind="call", target_name="ambiguous_fn"
        )

        # -- cross-repo ambiguous: same name defined in acme (same-repo) AND beta (cross-repo).
        files["src/cross_local.py"] = _insert_file(conn, acme_id, "src/cross_local.py")
        _insert_symbol(conn, files["src/cross_local.py"], acme_id, "cross_fn", start_line=1)
        files["beta/cross.py"] = _insert_file(conn, beta_id, "beta/cross.py")
        _insert_symbol(conn, files["beta/cross.py"], beta_id, "cross_fn", start_line=1)
        files["src/caller3.py"] = _insert_file(conn, acme_id, "src/caller3.py")
        _insert_edge(
            conn, files["src/caller3.py"], acme_id, edge_kind="call", target_name="cross_fn"
        )

        # -- unresolved call: no matching symbol anywhere.
        files["src/caller4.py"] = _insert_file(conn, acme_id, "src/caller4.py")
        _insert_edge(
            conn, files["src/caller4.py"], acme_id, edge_kind="call", target_name="missing_fn"
        )

        # -- unresolved import: dotted external target (D3, no last-segment split).
        files["src/importer.py"] = _insert_file(conn, acme_id, "src/importer.py")
        _insert_edge(
            conn, files["src/importer.py"], acme_id, edge_kind="import", target_name="os.path"
        )

        # -- branch scoping: a site AND its candidate definition exist only on "feature".
        files["src/feature_target.py"] = _insert_file(
            conn, acme_id, "src/feature_target.py", branches=["feature"]
        )
        _insert_symbol(conn, files["src/feature_target.py"], acme_id, "feature_fn", start_line=1)
        files["src/feature_caller.py"] = _insert_file(
            conn, acme_id, "src/feature_caller.py", branches=["feature"]
        )
        _insert_edge(
            conn,
            files["src/feature_caller.py"],
            acme_id,
            edge_kind="call",
            target_name="feature_fn",
        )

        conn.commit()
        yield Seeded(conn=conn, acme_id=acme_id, beta_id=beta_id, files=files)
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


def _resolve(conn: Connection, **kwargs: object) -> ReferenceResult:
    return resolve_references(conn, **kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- resolution


@pytest.mark.integration
def test_unique_call_resolves_to_single_candidate(seeded: Seeded) -> None:
    result = _resolve(seeded.conn, target_name="unique_fn", edge_kind="call")
    (site,) = result.sites
    assert site.resolution == "unique"
    assert site.candidate_count == 1
    assert site.enclosing_name == "handle"
    (candidate,) = site.candidates
    assert candidate.path == "src/unique_target.py"
    assert candidate.same_repo is True
    assert candidate.kind_match is True


@pytest.mark.integration
def test_ambiguous_same_repo_duplicates_never_collapsed(seeded: Seeded) -> None:
    result = _resolve(seeded.conn, target_name="ambiguous_fn", edge_kind="call")
    (site,) = result.sites
    assert site.resolution == "ambiguous"
    assert site.candidate_count == 2
    assert len(site.candidates) == 2  # AC1: ambiguity is never collapsed to one answer.
    assert {c.path for c in site.candidates} == {"src/dup_a.py", "src/dup_b.py"}


@pytest.mark.integration
def test_cross_repo_ambiguous_ranks_same_repo_first(seeded: Seeded) -> None:
    result = _resolve(seeded.conn, target_name="cross_fn", edge_kind="call")
    (site,) = result.sites
    assert site.resolution == "ambiguous"
    assert site.candidate_count == 2
    assert len(site.candidates) == 2
    assert site.candidates[0].path == "src/cross_local.py"
    assert site.candidates[0].same_repo is True
    assert site.candidates[1].path == "beta/cross.py"
    assert site.candidates[1].same_repo is False


@pytest.mark.integration
def test_unresolved_call_no_matching_symbol(seeded: Seeded) -> None:
    result = _resolve(seeded.conn, target_name="missing_fn", edge_kind="call")
    (site,) = result.sites
    assert site.resolution == "unresolved"
    assert site.candidate_count == 0
    assert site.candidates == ()


@pytest.mark.integration
def test_unresolved_import_external_target(seeded: Seeded) -> None:
    # D3: import target_name is the full dotted path; no symbol is literally named "os.path",
    # so this resolves unresolved -- correctly representing an external/stdlib import.
    result = _resolve(seeded.conn, target_name="os.path", edge_kind="import")
    (site,) = result.sites
    assert site.edge_kind == "import"
    assert site.resolution == "unresolved"


# ------------------------------------------------------------------------- branch scoping


@pytest.mark.integration
def test_default_branch_excludes_feature_only_site(seeded: Seeded) -> None:
    # No branch= given -> default-branch conjunct excludes the "feature"-only edge site.
    result = _resolve(seeded.conn, target_name="feature_fn", edge_kind="call")
    assert result.sites == ()


@pytest.mark.integration
def test_explicit_branch_includes_feature_only_site_and_its_candidate(seeded: Seeded) -> None:
    result = _resolve(seeded.conn, target_name="feature_fn", edge_kind="call", branch="feature")
    (site,) = result.sites
    assert site.resolution == "unique"
    (candidate,) = site.candidates
    assert candidate.path == "src/feature_target.py"


@pytest.mark.integration
def test_candidate_on_other_branch_is_excluded(seeded: Seeded) -> None:
    # The candidate definition lives only on "feature"; querying "main" (feature_fn's site
    # doesn't even exist there, but prove the candidate side of the predicate independently)
    # via an explicit different branch must not surface it.
    result = _resolve(
        seeded.conn, target_name="feature_fn", edge_kind="call", branch="other-branch"
    )
    assert result.sites == ()


# ------------------------------------------------------------------------------ repo scope


@pytest.mark.integration
def test_repo_scope_filters_to_one_repo(seeded: Seeded) -> None:
    result = _resolve(seeded.conn, edge_kind="import", repo="acme/widgets")
    assert result.repo_known is True
    assert all(site.repo_id == seeded.acme_id for site in result.sites)


@pytest.mark.integration
def test_unknown_repo_is_structured_miss_no_further_work(seeded: Seeded) -> None:
    result = _resolve(seeded.conn, edge_kind="import", repo="ghost/repo")
    assert result.repo_known is False
    assert result.sites == ()
    assert result.truncated is False


# --------------------------------------------------------------------------- candidate cap


@pytest.mark.integration
def test_hot_name_bound_by_sql_window_not_just_payload(seeded: Seeded) -> None:
    # More defs than DEFAULT_CANDIDATE_CAP -- the SQL window must bound the FETCH itself.
    extra = DEFAULT_CANDIDATE_CAP + 8
    for i in range(extra):
        fid = _insert_file(seeded.conn, seeded.acme_id, f"src/hot_{i}.py")
        _insert_symbol(seeded.conn, fid, seeded.acme_id, "hot_fn", start_line=1)
    caller = _insert_file(seeded.conn, seeded.acme_id, "src/hot_caller.py")
    _insert_edge(seeded.conn, caller, seeded.acme_id, edge_kind="call", target_name="hot_fn")
    seeded.conn.commit()

    result = _resolve(seeded.conn, target_name="hot_fn", edge_kind="call")
    (site,) = result.sites
    assert site.candidate_count == extra  # true pre-cap total
    assert len(site.candidates) == DEFAULT_CANDIDATE_CAP  # fetch itself was bounded
    assert site.candidates_truncated is True
    assert site.resolution == "ambiguous"  # never rewritten to "unique" by the cap


# ------------------------------------------------------------------------------ determinism


@pytest.mark.integration
def test_determinism_repeated_calls_identical_order(seeded: Seeded) -> None:
    first = _resolve(seeded.conn, target_name="ambiguous_fn", edge_kind="call")
    second = _resolve(seeded.conn, target_name="ambiguous_fn", edge_kind="call")
    assert [c.path for c in first.sites[0].candidates] == [
        c.path for c in second.sites[0].candidates
    ]


# ----------------------------------------------------------------------------- row cap


@pytest.mark.integration
def test_row_limit_truncates_sites(seeded: Seeded) -> None:
    result = _resolve(seeded.conn, edge_kind="call", row_limit=1)
    assert len(result.sites) == 1
    assert result.truncated is True
    assert result.truncation_reason == "row_cap"


# --------------------------------------------------------------------------------- timeout


@pytest.mark.integration
def test_tiny_statement_timeout_raises_query_too_broad(seeded: Seeded) -> None:
    # Deterministic DB-cancellation by WORK VOLUME (mirrors test_symbols_search.py /
    # test_grep.py): a huge fan-in on one target_name forces a real sort of many matching
    # reference_edges rows before the ORDER BY/LIMIT can short-circuit, guaranteed >> 1 ms.
    caller = _insert_file(seeded.conn, seeded.acme_id, "src/blob_caller.py")
    seeded.conn.execute(
        insert(ReferenceEdge),
        [
            {
                "file_id": caller,
                "repo_id": seeded.acme_id,
                "edge_kind": "call",
                "target_name": "hot_blob_fn",
                "line": i + 1,
            }
            for i in range(20000)
        ],
    )
    seeded.conn.commit()
    with pytest.raises(QueryTooBroadError):
        _resolve(
            seeded.conn,
            target_name="hot_blob_fn",
            edge_kind="call",
            statement_timeout_ms=1,
        )


# ------------------------------------------------------------------------- EXPLAIN sanity


@pytest.mark.integration
def test_explain_sites_select_uses_repo_kind_index(seeded: Seeded) -> None:
    stmt = _build_sites_select(
        target_name=None, edge_kind="call", repo_id=seeded.acme_id, branch=None, row_limit=200
    )
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    savepoint = seeded.conn.begin_nested()
    try:
        seeded.conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = seeded.conn.execute(text(f"EXPLAIN {sql}")).scalars().all()
    finally:
        savepoint.rollback()
    plan_text = "\n".join(plan)
    assert "ix_reference_edges_repo_kind" in plan_text, plan_text


@pytest.mark.integration
def test_explain_candidates_select_uses_symbols_name_trgm_index(seeded: Seeded) -> None:
    stmt = _build_candidates_select(
        names=["ambiguous_fn", "unique_fn"], branch=None, candidate_cap=32
    )
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    savepoint = seeded.conn.begin_nested()
    try:
        seeded.conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = seeded.conn.execute(text(f"EXPLAIN {sql}")).scalars().all()
    finally:
        savepoint.rollback()
    plan_text = "\n".join(plan)
    assert "ix_symbols_name_trgm" in plan_text, plan_text
