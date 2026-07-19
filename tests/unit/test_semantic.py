"""Unit tests for app.search.semantic: flag-off no-op, RRF SQL shape, vector literal.

No DB, no SDK. The flag-off contract is proven two ways: the payload builder returns the
feature-absent envelope BEFORE touching a (poisoned) engine, and ``databricks.sdk`` is absent
from ``sys.modules`` after the call (a constructed-but-unused embedder would still import the
SDK, so the short-circuit must return before the embedder is even built). The RRF builder is
pinned to the shared fusion wrapper + the correct per-backend leg, and ``format_vector_literal``
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
@pytest.mark.parametrize("backend", ["lakebase", "standin"])
def test_rrf_sql_shared_wrapper(backend: str) -> None:
    sql = str(semantic.build_hybrid_rrf_sql(backend))

    # Shared fusion wrapper: two rank CTEs, index-friendly inner LIMIT, FULL OUTER JOIN, RRF sum.
    assert "ann AS (" in sql and "bm AS (" in sql
    # Both legs break rank ties on id: arbitrary rank assignment among equal scores would
    # change each row's 1/(k+rank) contribution, so the same query over the same corpus
    # could otherwise fuse to a different order (issues #9/#13 determinism rule).
    assert sql.count("row_number() OVER (ORDER BY metric, id)") == 2
    # The ANN leg's INNER order stays single-expression -- adding `, id` there would defeat
    # the lakebase_ann/pgvector index ordering, and approximate-index set membership is
    # nondeterministic regardless. The BM25 leg (scores plateau hard) does get the tiebreak.
    assert "ORDER BY embedding <=> (:qvec)::vector LIMIT :topk" in sql
    # NULL embeddings never earn RRF credit.
    assert "WHERE embedding IS NOT NULL" in sql
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


@pytest.mark.unit
def test_rrf_sql_lakebase_leg_uses_bm25_scorer() -> None:
    sql = str(semantic.build_hybrid_rrf_sql("lakebase"))
    scorer = "ts <@> to_bm25query(to_tsvector('english', :qtext), 'ix_chunks_ts_bm25'::regclass)"
    # Repeated in the inner SELECT and its ORDER BY so the bm25 index serves the ordering.
    assert sql.count(scorer) == 2
    assert "ts_rank_cd" not in sql


@pytest.mark.unit
def test_rrf_sql_standin_leg_uses_negated_ts_rank_cd() -> None:
    sql = str(semantic.build_hybrid_rrf_sql("standin"))
    # Negated so ASC-orders-best, keeping the shared wrapper's ORDER BY direction identical.
    assert sql.count("- ts_rank_cd(ts, plainto_tsquery('english', :qtext))") == 2
    assert "to_bm25query" not in sql
    # The ANN leg is byte-identical to the lakebase backend's (pgvector `<=>` == lakebase_ann).
    assert sql.count("embedding <=> (:qvec)::vector") == 2


@pytest.mark.unit
def test_rrf_sql_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unknown semantic backend"):
        semantic.build_hybrid_rrf_sql("bogus")


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
    """Returns canned results by call order: [backend probe, chunks probe, RRF rows]."""

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
def test_enabled_payload_shape_standin(monkeypatch: pytest.MonkeyPatch) -> None:
    # No lakebase_bm25 am (empty probe) -> standin backend; embedder is faked (no SDK).
    monkeypatch.setattr(semantic, "get_embedder", lambda cfg: lambda texts: [[0.1, 0.2]])
    engine = _FakeEngine(
        [
            _FakeResult([]),  # detect_backend probe: no lakebase_bm25 -> standin
            _FakeResult(["chunks"]),  # to_regclass('chunks') -> schema present
            _FakeResult(
                [
                    _Row(
                        id=1,
                        repo="acme/widgets",
                        path="src/a.py",
                        chunk_index=0,
                        content="x",
                        rrf_score=0.5,
                    )
                ]
            ),
        ]
    )

    payload = semantic._semantic_search_payload(engine, _cfg(enabled=True), "auth flow", 50)

    assert payload["semantic_enabled"] is True
    assert payload["backend"] == "standin"
    assert payload["count"] == 1
    assert payload["results"] == [
        {
            "repo": "acme/widgets",
            "file": "src/a.py",
            "chunk_index": 0,
            "content": "x",
            "rrf_score": 0.5,
        }
    ]
    # Transaction-local timeout set on BOTH transactions (capability probe, then the query),
    # matching the other builders; it never leaks onto the pooled connection.
    assert engine._conn.driver_sql == [
        "SET LOCAL statement_timeout = 5000",
        "SET LOCAL statement_timeout = 5000",
    ]


@pytest.mark.unit
def test_enabled_but_not_migrated_returns_structured_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag on before `make migrate-semantic` is recoverable, not a 500.

    A missing gated migration is operator-fixable and belongs in the same class as
    query_too_broad -- a payload field, never an exception (app/main.py's contract).
    It must also short-circuit BEFORE the embedder is called, so discovering the
    misconfiguration costs no embedding spend.
    """

    def _never(_cfg: Any) -> Any:
        raise AssertionError("embedder must not be built when the schema is absent")

    monkeypatch.setattr(semantic, "get_embedder", _never)
    engine = _FakeEngine(
        [
            _FakeResult([]),  # no lakebase_bm25 -> standin
            _FakeResult([]),  # to_regclass('chunks') -> NULL: migration not run
        ]
    )

    payload = semantic._semantic_search_payload(engine, _cfg(enabled=True), "auth flow", 50)

    assert payload["semantic_enabled"] is True
    assert payload["semantic_schema_missing"] is True
    assert payload["results"] == []
    assert payload["count"] == 0
    assert "migrate-semantic" in payload["reason"]


@pytest.mark.unit
def test_enabled_payload_detects_lakebase_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(semantic, "get_embedder", lambda cfg: lambda texts: [[0.1, 0.2]])
    engine = _FakeEngine(
        [
            _FakeResult([_Row(x=1)]),  # pg_am probe hit -> lakebase
            _FakeResult(["chunks"]),  # schema present
            _FakeResult([]),  # RRF: no rows
        ]
    )

    payload = semantic._semantic_search_payload(engine, _cfg(enabled=True), "auth", 50)
    assert payload["backend"] == "lakebase"
    assert payload["results"] == []
    assert payload["count"] == 0
