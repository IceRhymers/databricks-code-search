"""Unit tests for the reference resolver: pure helpers + rendered SQL.

No DB: SQL shapes are asserted via ``stmt.compile(dialect=postgresql.dialect())`` (mirrors
``test_symbols_search.py``'s style), and the row -> dataclass assembly (``_build_edge_site``)
is exercised with fake row objects so the candidate-cap/ambiguity-preservation invariant is
covered without a live Postgres. The full two-query ``resolve_references`` end-to-end (branch
scoping, timeout, real window-function bounding) is exercised in the CI-only integration suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.search.references import (
    CALL_TARGET_KINDS,
    CandidateSymbol,
    _build_candidates_select,
    _build_edge_site,
    _build_sites_select,
    _rank_candidates,
    build_candidate_count_select,
    classify_resolution,
)


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# --------------------------------------------------------------------- classify_resolution


@pytest.mark.unit
@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "unresolved"), (1, "unique"), (2, "ambiguous"), (31, "ambiguous")],
)
def test_classify_resolution(count: int, expected: str) -> None:
    assert classify_resolution(count) == expected


# ------------------------------------------------------------------------- _rank_candidates


def _candidate(
    *,
    symbol_id: int,
    repo_id: int = 1,
    path: str = "a.py",
    name: str = "f",
    kind: str | None = "function",
    start_line: int | None = 1,
    same_repo: bool = True,
    same_file: bool = True,
    kind_match: bool = True,
) -> CandidateSymbol:
    return CandidateSymbol(
        symbol_id=symbol_id,
        repo_id=repo_id,
        path=path,
        name=name,
        kind=kind,
        start_line=start_line,
        same_repo=same_repo,
        same_file=same_file,
        kind_match=kind_match,
    )


@pytest.mark.unit
def test_rank_same_repo_before_cross_repo() -> None:
    cross = _candidate(symbol_id=1, same_repo=False, repo_id=2)
    same = _candidate(symbol_id=2, same_repo=True, repo_id=1)
    ranked = _rank_candidates([cross, same])
    assert ranked == (same, cross)


@pytest.mark.unit
def test_rank_kind_match_before_same_file() -> None:
    # Both same-repo; one is same-file but kind-mismatched, the other is kind-matched but a
    # different file -- kind_match outranks same_file per the pinned D4 signal order.
    same_file_wrong_kind = _candidate(symbol_id=1, same_file=True, kind_match=False)
    other_file_right_kind = _candidate(symbol_id=2, same_file=False, kind_match=True)
    ranked = _rank_candidates([same_file_wrong_kind, other_file_right_kind])
    assert ranked == (other_file_right_kind, same_file_wrong_kind)


@pytest.mark.unit
def test_rank_tiebreak_ends_in_symbol_id() -> None:
    # Identical repo/path/start_line -- symbol_id is the only thing left to break the tie, so
    # the order must be deterministic across repeated calls.
    a = _candidate(symbol_id=5, path="same.py", start_line=10)
    b = _candidate(symbol_id=2, path="same.py", start_line=10)
    assert _rank_candidates([a, b]) == (b, a)
    assert _rank_candidates([b, a]) == (b, a)


@pytest.mark.unit
def test_rank_unknown_kind_still_present_not_dropped() -> None:
    # Membership-preserving: an unmatched kind earns no boost but is never removed.
    unknown_kind = _candidate(symbol_id=1, kind="unknown_future_kind", kind_match=False)
    ranked = _rank_candidates([unknown_kind])
    assert ranked == (unknown_kind,)
    assert unknown_kind.kind not in CALL_TARGET_KINDS


# ------------------------------------------------------------------------ query 1 SQL shape


@pytest.mark.unit
def test_sites_select_orders_through_file_repo_id_ends_in_edge_id() -> None:
    sql = _sql(
        _build_sites_select(
            target_name=None, edge_kind=None, repo_id=None, branch=None, row_limit=200
        )
    )
    order = sql.split("ORDER BY", 1)[1].split("LIMIT", 1)[0]
    assert "files.repo_id" in order
    assert "files.path" in order
    assert "reference_edges.line" in order
    assert order.strip().endswith("reference_edges.id")
    assert "content_sha" not in sql


@pytest.mark.unit
def test_sites_select_default_branch_predicate_byte_identical_to_get_file_payload() -> None:
    sql = _sql(
        _build_sites_select(
            target_name=None, edge_kind=None, repo_id=None, branch=None, row_limit=200
        )
    )
    assert "coalesce(repos.default_branch, %(coalesce_1)s) = ANY (files.branches)" in sql


@pytest.mark.unit
def test_sites_select_explicit_branch_predicate_uses_array_contains() -> None:
    sql = _sql(
        _build_sites_select(
            target_name=None, edge_kind=None, repo_id=None, branch="feature", row_limit=200
        )
    )
    assert "files.branches @>" in sql


@pytest.mark.unit
def test_sites_select_filters_compose() -> None:
    sql = _sql(
        _build_sites_select(
            target_name="Handler", edge_kind="call", repo_id=None, branch=None, row_limit=200
        )
    )
    assert "reference_edges.target_name = " in sql
    assert "reference_edges.edge_kind = " in sql


@pytest.mark.unit
def test_sites_select_repo_id_filter_renders_edge_repo_id_not_repo_name() -> None:
    sql = _sql(
        _build_sites_select(target_name=None, edge_kind=None, repo_id=7, branch=None, row_limit=200)
    )
    assert "reference_edges.repo_id = " in sql
    assert "repos.name = " not in sql


@pytest.mark.unit
def test_sites_select_applies_limit() -> None:
    sql = _sql(
        _build_sites_select(
            target_name=None, edge_kind=None, repo_id=None, branch=None, row_limit=50
        )
    )
    assert "LIMIT" in sql


# ------------------------------------------------------------------------ query 2 SQL shape


@pytest.mark.unit
def test_candidates_select_row_number_partitioned_by_name_bounded_by_cap() -> None:
    sql = _sql(_build_candidates_select(names=["f"], branch=None, candidate_cap=32))
    assert "row_number() OVER (PARTITION BY symbols.name ORDER BY " in sql
    assert "files.repo_id, files.path, symbols.start_line, symbols.id)" in sql
    assert "rn <=" in sql or "rn <= " in sql


@pytest.mark.unit
def test_candidates_select_count_over_partitioned_by_name() -> None:
    sql = _sql(_build_candidates_select(names=["f"], branch=None, candidate_cap=32))
    assert "count(*) OVER (PARTITION BY symbols.name)" in sql


@pytest.mark.unit
def test_candidates_select_exact_name_in_no_last_segment_split() -> None:
    stmt = _build_candidates_select(names=["a.b.c", "f"], branch=None, candidate_cap=32)
    # literal_binds so the bound names are visible in the rendered text: the full dotted
    # "a.b.c" must appear verbatim -- no split into "c" (the last segment) anywhere.
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "symbols.name IN" in sql
    assert "'a.b.c'" in sql
    assert "'c'" not in sql


@pytest.mark.unit
def test_candidates_select_applies_branch_predicate() -> None:
    sql = _sql(_build_candidates_select(names=["f"], branch="feature", candidate_cap=32))
    assert "files.branches @>" in sql


# --------------------------------------------------------------------- build_candidate_count_select


@pytest.mark.unit
def test_candidate_count_select_renders_correlated_subquery_and_branch_scope() -> None:
    sql = _sql(build_candidate_count_select(edge_kind="call", branch=None))
    assert "reference_edges.edge_kind = " in sql
    # Two DISTINCT joins to `files` in one statement (outer sites leg + inner correlated
    # symbols leg) -> the inner leg must be aliased, never the same unaliased `files`.
    assert sql.count("JOIN files AS files_1") == 1
    assert sql.count("JOIN files ON") == 1
    assert "coalesce(" in sql.lower()


@pytest.mark.unit
def test_candidate_count_select_explicit_branch() -> None:
    sql = _sql(build_candidate_count_select(edge_kind="import", branch="feature"))
    assert "@>" in sql
    assert "reference_edges.edge_kind = " in sql


# ------------------------------------------------------------------- _build_edge_site (D2/D5)


@pytest.mark.unit
def test_build_edge_site_candidate_cap_preserves_true_count_and_ambiguous_resolution() -> None:
    site_row = _Row(
        id=1,
        repo_id=1,
        file_id=10,
        path="a.py",
        line=5,
        edge_kind="call",
        target_name="get",
        enclosing_name=None,
        enclosing_kind=None,
    )
    # Fetched/returned rows are already SQL-bounded to candidate_cap (here: 2), but the
    # `candidate_count` column carries the TRUE pre-cap total (here: 40) on every row.
    candidate_rows = [
        _Row(
            symbol_id=i,
            name="get",
            kind="function",
            start_line=i,
            repo_id=1,
            file_id=10 + i,
            path=f"c{i}.py",
            candidate_count=40,
        )
        for i in range(2)
    ]
    site = _build_edge_site(site_row, candidate_rows)  # type: ignore[arg-type]
    assert site.candidate_count == 40
    assert len(site.candidates) == 2
    assert site.candidates_truncated is True
    assert site.resolution == "ambiguous"


@pytest.mark.unit
def test_build_edge_site_no_candidates_is_unresolved() -> None:
    site_row = _Row(
        id=1,
        repo_id=1,
        file_id=10,
        path="a.py",
        line=5,
        edge_kind="import",
        target_name="os.path",
        enclosing_name=None,
        enclosing_kind=None,
    )
    site = _build_edge_site(site_row, [])  # type: ignore[arg-type]
    assert site.resolution == "unresolved"
    assert site.candidate_count == 0
    assert site.candidates == ()
    assert site.candidates_truncated is False


@pytest.mark.unit
def test_build_edge_site_import_kind_match_always_false() -> None:
    site_row = _Row(
        id=1,
        repo_id=1,
        file_id=10,
        path="a.py",
        line=5,
        edge_kind="import",
        target_name="f",
        enclosing_name=None,
        enclosing_kind=None,
    )
    candidate_rows = [
        _Row(
            symbol_id=1,
            name="f",
            kind="function",
            start_line=1,
            repo_id=1,
            file_id=10,
            path="a.py",
            candidate_count=1,
        )
    ]
    site = _build_edge_site(site_row, candidate_rows)  # type: ignore[arg-type]
    assert site.candidates[0].kind_match is False


@pytest.mark.unit
def test_build_edge_site_enclosing_symbol_none_when_module_scope() -> None:
    site_row = _Row(
        id=1,
        repo_id=1,
        file_id=10,
        path="a.py",
        line=5,
        edge_kind="call",
        target_name="f",
        enclosing_name=None,
        enclosing_kind=None,
    )
    site = _build_edge_site(site_row, [])  # type: ignore[arg-type]
    assert site.enclosing_name is None
    assert site.enclosing_kind is None
