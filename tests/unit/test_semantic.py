"""Unit tests for app.search.semantic: flag-off no-op, RRF SQL shape, vector literal.

No DB, no SDK. The flag-off contract is proven two ways: the payload builder returns the
feature-absent envelope BEFORE touching a (poisoned) engine, and ``databricks.sdk`` is absent
from ``sys.modules`` after the call (a constructed-but-unused embedder would still import the
SDK, so the short-circuit must return before the embedder is even built). The RRF builder is
pinned to the fusion wrapper + the lakebase leg operators, and ``format_vector_literal``
is proven to use ``repr`` (never the invalid ``format(x, "r")`` code) on scientific notation.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from app.config import Settings
from app.search import semantic


def _cfg(*, enabled: bool) -> Settings:
    """A deterministic Settings instance (never reads the real environment)."""
    return Settings(
        lakebase_endpoint=None,
        statement_timeout_ms=5000,
        semantic_enabled=enabled,
    )


class _PoisonedEngine:
    """Any attribute access raises: proves the flag-off path never touches the engine."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"flag-off path must not touch the engine (accessed {name!r})")


# ------------------------------------------------------------------- flag-off no-op (A1)


@pytest.mark.unit
def test_flag_off_returns_disabled_payload_and_never_imports_sdk() -> None:
    # Popped first (same pattern as test_embed): another test file may have already
    # imported the SDK, and this assertion is about THIS call not importing it.
    sys.modules.pop("databricks.sdk", None)
    payload = semantic._semantic_search_payload(_PoisonedEngine(), _cfg(enabled=False), "auth", 10)

    assert payload["semantic_enabled"] is False
    assert payload["results"] == []
    assert payload["count"] == 0
    assert payload["query"] == "auth"
    assert "reason" in payload
    # The short-circuit returns before the embedder is built, so the SDK stays unimported.
    assert "databricks.sdk" not in sys.modules


# --------------------------------------------------------------------- vector literal (M2)


@pytest.mark.unit
def test_format_vector_literal_uses_repr_incl_scientific_notation() -> None:
    # repr never raises on floats (format(x, "r") would raise ValueError -- invalid code);
    # scientific notation must round-trip into a bracketed literal pgvector can parse.
    literal = semantic.format_vector_literal([1e-05, 0.5, -3.0, 2.0])
    assert literal == "[1e-05,0.5,-3.0,2.0]"
    assert literal.startswith("[") and literal.endswith("]")


@pytest.mark.unit
def test_format_vector_literal_empty() -> None:
    assert semantic.format_vector_literal([]) == "[]"


# ------------------------------------------------------------------------ RRF SQL shape (M1)


@pytest.mark.unit
def test_rrf_sql_fusion_wrapper() -> None:
    sql = str(semantic.build_hybrid_rrf_sql())

    # Fusion wrapper: two rank CTEs, index-friendly inner LIMIT, FULL OUTER JOIN, RRF sum.
    assert "ann AS (" in sql and "bm AS (" in sql
    # Both legs break rank ties on id: arbitrary rank assignment among equal scores would
    # change each row's 1/(k+rank) contribution, so the same query over the same corpus
    # could otherwise fuse to a different order (issues #9/#13 determinism rule).
    assert sql.count("row_number() OVER (ORDER BY metric, id)") == 2
    # NEITHER leg's inner ORDER BY may carry a second sort key. Postgres cannot build an
    # ordered-index path when one follows an ORDER BY-operator key, and the fallback is not
    # cost-based -- it seq-scans and full-sorts even with enable_seqscan=off. This SQL-shape
    # guard is the fast tripwire; the plan itself is only observable on a Lakebase branch.
    assert "ORDER BY c.embedding <=> (:qvec)::vector LIMIT :topk" in sql
    assert ", id LIMIT :topk" not in sql
    # The ANN metric appears in the inner SELECT and its ORDER BY (index-served ordering).
    assert sql.count("c.embedding <=> (:qvec)::vector") == 2
    # NULL embeddings never earn RRF credit.
    assert "AND c.embedding IS NOT NULL" in sql
    # Each leg's candidate cap lives in an INNER subquery whose ORDER BY repeats the metric
    # EXPRESSION (not the alias) -- that is what lets the ANN/BM25 index serve the ordering.
    assert sql.count("LIMIT :topk) s)") == 2
    assert "FULL OUTER JOIN bm USING (id)" in sql
    assert "coalesce(1.0 / (:k + ann.rank), 0) + coalesce(1.0 / (:k + bm.rank), 0)" in sql
    # The `, id` tiebreak is load-bearing: RRF scores tie constantly, so without it WHICH
    # tied rows survive this inner LIMIT is unspecified and the outer ORDER BY would be
    # deterministically sorting a nondeterministic set (issues #9/#13 determinism rule).
    assert "ORDER BY rrf DESC, id LIMIT :lim" in sql
    # Envelope join back to chunks -> files -> repos.
    assert "JOIN files f ON f.id = c.file_id" in sql
    assert "JOIN repos r ON r.id = f.repo_id" in sql
    # The query vector is a bound param cast, never interpolated.
    assert "(:qvec)::vector" in sql
    assert ":qtext" in sql
    # Default (no branch=) scopes each leg to its chunk's own repo's default branch --
    # byte-identical coalesce to the query compiler's implicit conjunct / 0003 backfill.
    assert sql.count("coalesce(r.default_branch, 'HEAD') = ANY(f.branches)") == 2


@pytest.mark.unit
def test_rrf_sql_bm_leg_uses_bm25_scorer() -> None:
    sql = str(semantic.build_hybrid_rrf_sql())
    scorer = "c.ts <@> to_bm25query(to_tsvector('english', :qtext), 'ix_chunks_ts_bm25'::regclass)"
    # Repeated in the inner SELECT and its ORDER BY so the bm25 index serves the ordering.
    assert sql.count(scorer) == 2
    assert "ts_rank_cd" not in sql


# --------------------------------------------------------- branch-scoped leg (0003, D1)


@pytest.mark.unit
def test_rrf_sql_explicit_branch_uses_gin_served_membership() -> None:
    sql = str(semantic.build_hybrid_rrf_sql(branch="feature/x"))
    # Explicit branch: opts into the GIN-served exact-membership predicate on BOTH legs,
    # and the default coalesce predicate must not also be present.
    assert sql.count("f.branches @> ARRAY[:branch]") == 2
    assert "coalesce(r.default_branch" not in sql


@pytest.mark.unit
def test_rrf_sql_branch_predicate_is_where_never_order_by() -> None:
    # The branch predicate must never leak into either leg's ORDER BY -- that would cost the
    # ANN/BM25 index a second sort key (see _leg_cte). It belongs only in the inner WHERE.
    sql = str(semantic.build_hybrid_rrf_sql(branch="feature/x"))
    for order_by_clause in [
        "ORDER BY c.embedding <=> (:qvec)::vector LIMIT :topk",
        "ORDER BY c.ts <@> to_bm25query(to_tsvector('english', :qtext), "
        "'ix_chunks_ts_bm25'::regclass) LIMIT :topk",
    ]:
        assert order_by_clause in sql
        assert "branches" not in order_by_clause


@pytest.mark.unit
def test_rrf_sql_inner_subquery_is_c_qualified_outer_window_stays_bare() -> None:
    """Compile-time proof of the qualification-scope contract (0003, D1 -- review-hardened).

    The INNER subquery (which joins chunks/files/repos for branch scoping) must qualify its
    id/metric columns `c.`; the OUTER row_number() window selects from the derived table `s`
    (columns `id`/`metric` only) and must stay BARE. Catches both directions: under-qualifying
    the inner id (ambiguous against files.id/repos.id) and over-qualifying the outer window
    (`c.id` there is invalid SQL -- "missing FROM-clause entry for table c").
    """
    sql = str(semantic.build_hybrid_rrf_sql())
    # Inner projection: c.-qualified id and metric, sourced from the 3-way join.
    assert sql.count("SELECT c.id AS id,") == 2
    assert "FROM chunks c JOIN files f ON f.id = c.file_id JOIN repos r ON r.id = f.repo_id" in sql
    # Outer window: bare id/metric over the derived table `s`, never c.id.
    assert sql.count("SELECT id, row_number() OVER (ORDER BY metric, id) AS rank FROM (") == 2
    assert "SELECT c.id, row_number()" not in sql
    assert "ORDER BY c.metric" not in sql


# -------------------------------------------------------------- enabled-path envelope shaping


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def scalar(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return self._rows


class _FakeConn:
    """Returns canned results by call order: [chunks probe, RRF rows]."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.driver_sql: list[str] = []

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeConn:
        return self

    def exec_driver_sql(self, sql: str) -> None:
        self.driver_sql.append(sql)

    def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        return self._results.pop(0)


class _FakeEngine:
    def __init__(self, results: list[Any]) -> None:
        self._conn = _FakeConn(results)

    def connect(self) -> _FakeConn:
        return self._conn


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


@pytest.mark.unit
def test_enabled_payload_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    # Embedder is faked (no SDK, no gateway call).
    monkeypatch.setattr(semantic, "get_embedder", lambda cfg: lambda texts: [[0.1, 0.2]])
    engine = _FakeEngine(
        [
            _FakeResult(["chunks"]),  # to_regclass('chunks') -> schema present
            _FakeResult(
                [
                    _Row(
                        id=1,
                        repo="acme/widgets",
                        path="src/a.py",
                        chunk_index=0,
                        content="x",
                        start_line=10,
                        end_line=24,
                        rrf_score=0.5,
                    )
                ]
            ),
        ]
    )

    payload = semantic._semantic_search_payload(engine, _cfg(enabled=True), "auth flow", 50)

    assert payload["semantic_enabled"] is True
    assert payload["count"] == 1
    assert payload["results"] == [
        {
            "repo": "acme/widgets",
            "file": "src/a.py",
            "chunk_index": 0,
            "content": "x",
            "start_line": 10,
            "end_line": 24,
            "rrf_score": 0.5,
        }
    ]
    # Transaction-local timeout set on BOTH transactions (schema probe, then the query),
    # matching the other builders; it never leaks onto the pooled connection.
    assert engine._conn.driver_sql == [
        "SET LOCAL statement_timeout = 5000",
        "SET LOCAL statement_timeout = 5000",
    ]


@pytest.mark.unit
def test_enabled_but_not_migrated_returns_structured_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on with core migrations behind 0004 is recoverable, not a 500.

    A missing chunks migration is operator-fixable and belongs in the same class as
    query_too_broad -- a payload field, never an exception (app/main.py's contract).
    It must also short-circuit BEFORE the embedder is called, so discovering the
    misconfiguration costs no embedding spend.
    """

    def _never(_cfg: Any) -> Any:
        raise AssertionError("embedder must not be built when the schema is absent")

    monkeypatch.setattr(semantic, "get_embedder", _never)
    engine = _FakeEngine(
        [
            _FakeResult([]),  # to_regclass('chunks') -> NULL: migration not run
        ]
    )

    payload = semantic._semantic_search_payload(engine, _cfg(enabled=True), "auth flow", 50)

    assert payload["semantic_enabled"] is True
    assert payload["semantic_schema_missing"] is True
    assert payload["results"] == []
    assert payload["count"] == 0
    assert "make migrate" in payload["reason"]
