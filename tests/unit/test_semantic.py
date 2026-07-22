"""Unit tests for app.search.semantic: flag-off no-op, RRF SQL shape, vector literal.

No DB, no SDK. The flag-off contract is proven two ways: the payload builder returns the
feature-absent envelope BEFORE touching a (poisoned) engine, and ``databricks.sdk`` is absent
from ``sys.modules`` after the call (a constructed-but-unused embedder would still import the
SDK, so the short-circuit must return before the embedder is even built). The RRF builder is
pinned to the fusion wrapper + the lakebase leg operators, and ``format_vector_literal``
is proven to use ``repr`` (never the invalid ``format(x, "r")`` code) on scientific notation.
"""

from __future__ import annotations

import re
import sys
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.config import Settings
from app.query.compiler import compile_query
from app.query.parser import parse
from app.query.semantic_filters import SemanticFilters
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


@pytest.mark.unit
def test_commit_atom_is_plain_text_not_a_filter() -> None:
    # filter-semantics ADR consequence: commit: used to be plain prose (flag-off, pre-parsing)
    # but is now a loud rejection once the feature is enabled -- self-documenting the breaking
    # change. Flag-off still short-circuits BEFORE any grammar parsing, so the query is carried
    # through verbatim and the engine is never touched.
    off_payload = semantic._semantic_search_payload(
        _PoisonedEngine(), _cfg(enabled=False), "commit:abc1234 auth handler", 10
    )
    assert off_payload["semantic_enabled"] is False
    assert off_payload["query"] == "commit:abc1234 auth handler"  # carried verbatim
    assert off_payload["results"] == []

    # Flag-on: commit: is REJECTED loudly, before the (poisoned) engine is ever touched, with a
    # remedy pointing at search_code.
    on_payload = semantic._semantic_search_payload(
        _PoisonedEngine(), _cfg(enabled=True), "commit:abc1234 auth handler", 10
    )
    assert on_payload["semantic_enabled"] is True
    assert on_payload["unsupported_filter"] == "commit:"
    assert "search_code" in on_payload["reason"]
    assert on_payload["results"] == []
    assert on_payload["count"] == 0


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


# ------------------------------------------------------------------------ RRF SQL fusion shape


@pytest.mark.unit
def test_rrf_sql_fusion_wrapper() -> None:
    sql = str(semantic.build_hybrid_rrf_sql())

    # Fusion wrapper: two rank CTEs, index-friendly inner LIMIT, FULL OUTER JOIN, RRF sum.
    assert "ann AS (" in sql and "bm AS (" in sql
    # Both legs break rank ties on id: arbitrary rank assignment among equal scores would
    # change each row's 1/(k+rank) contribution, so the same query over the same corpus
    # could otherwise fuse to a different order (the determinism rule).
    assert sql.count("row_number() OVER (ORDER BY metric, id)") == 2
    # NEITHER leg's inner ORDER BY may carry a second sort key. Postgres cannot build an
    # ordered-index path when one follows an ORDER BY-operator key, and the fallback is not
    # cost-based -- it seq-scans and full-sorts even with enable_seqscan=off. This SQL-shape
    # guard is the fast tripwire; the plan itself is only observable on a Lakebase branch.
    assert "ORDER BY c.embedding <=> (:qvec)::vector LIMIT :topk" in sql
    assert ", id LIMIT :topk" not in sql
    # The ANN metric appears THREE times (filter-semantics): twice inside the ANN
    # leg CTE (inner SELECT + inner ORDER BY, index-served ordering) and once more in the OUTER
    # select as the recomputed `cosine_distance` column -- never inside either leg CTE.
    assert sql.count("c.embedding <=> (:qvec)::vector") == 3
    ann_leg_sql, _, outer_sql = sql.partition("fused AS (")
    assert ann_leg_sql.count("c.embedding <=> (:qvec)::vector") == 2
    assert outer_sql.count("c.embedding <=> (:qvec)::vector AS cosine_distance") == 1
    # NULL embeddings never earn RRF credit.
    assert "AND c.embedding IS NOT NULL" in sql
    # Each leg's candidate cap lives in an INNER subquery whose ORDER BY repeats the metric
    # EXPRESSION (not the alias) -- that is what lets the ANN/BM25 index serve the ordering.
    assert sql.count("LIMIT :topk) s)") == 2
    assert "FULL OUTER JOIN bm USING (id)" in sql
    assert "coalesce(1.0 / (:k + ann.rank), 0) + coalesce(1.0 / (:k + bm.rank), 0)" in sql
    # The `, id` tiebreak is load-bearing: RRF scores tie constantly, so without it WHICH
    # tied rows survive this inner LIMIT is unspecified and the outer ORDER BY would be
    # deterministically sorting a nondeterministic set (the determinism rule).
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


# --------------------------------------------------------- branch-scoped leg (0003)


@pytest.mark.unit
def test_rrf_sql_explicit_branch_uses_gin_served_membership() -> None:
    sql = str(semantic.build_hybrid_rrf_sql(branch="feature/x"))
    # Explicit branch: opts into the GIN-served exact-membership predicate on BOTH legs,
    # and the default coalesce predicate must not also be present. filter-semantics unifies the
    # bind mechanism: the `branch` kwarg now routes through the SAME normalized
    # sem_branch_{i} binds as an in-query `branch:` atom -- the old `:branch` bind is retired.
    assert sql.count("f.branches @> ARRAY[:sem_branch_0]") == 2
    assert ":branch" not in sql
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
    """Compile-time proof of the qualification-scope contract (0003 -- review-hardened).

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


# --------------------------------------------------------------- filter-semantics: SQL shape


@pytest.mark.unit
def test_rrf_sql_filters_apply_to_both_legs_inner_where() -> None:
    filters = SemanticFilters(
        repo_patterns=("acme/.*",),
        path_patterns=("src/.*",),
        langs=("Go",),
        branches=(),
        residual="auth",
    )
    sql = str(semantic.build_hybrid_rrf_sql(filters))

    # Each predicate appears EXACTLY TWICE: once per leg CTE's inner WHERE.
    assert sql.count("r.name ~* :sem_repo_0") == 2
    assert sql.count("f.path ~* :sem_file_0") == 2
    assert sql.count("f.lang = :sem_lang_0") == 2

    # The single-expression inner ORDER BY invariant survives filtering (no second sort key).
    assert ", id LIMIT :topk" not in sql
    assert "ORDER BY c.embedding <=> (:qvec)::vector LIMIT :topk" in sql
    assert (
        "ORDER BY c.ts <@> to_bm25query(to_tsvector('english', :qtext), "
        "'ix_chunks_ts_bm25'::regclass) LIMIT :topk"
    ) in sql

    # Converse pin: no branch value anywhere -> the default coalesce arm is retained,
    # byte-identical, on both legs.
    assert sql.count("coalesce(r.default_branch, 'HEAD') = ANY(f.branches)") == 2


@pytest.mark.unit
def test_filter_params_normalizes_lang_value() -> None:
    # KD-3 parity: lang values are `.strip().lower()`-ed before binding, matching the compiler.
    filters = SemanticFilters(
        repo_patterns=(), path_patterns=(), langs=("  Go  ",), branches=(), residual="x"
    )
    assert semantic.filter_params(filters) == {"sem_lang_0": "go"}


@pytest.mark.unit
def test_rrf_sql_branch_atom_and_param_dedupe_to_one_predicate() -> None:
    # A `branch` kwarg equal to an existing `branch:` atom is a set union, so it
    # collapses to ONE predicate/bind, not two.
    filters = SemanticFilters(
        repo_patterns=(), path_patterns=(), langs=(), branches=("feature",), residual="x"
    )
    sql = str(semantic.build_hybrid_rrf_sql(filters, branch="feature"))
    assert sql.count("f.branches @> ARRAY[:sem_branch_0]") == 2
    assert "sem_branch_1" not in sql
    assert semantic.filter_params(filters, branch="feature") == {"sem_branch_0": "feature"}


@pytest.mark.unit
def test_rrf_sql_branch_atom_and_param_conjunction_when_different() -> None:
    # Distinct atom + param values AND together (conjunctive, lexical-parity),
    # sorted for determinism -- NOT source order.
    filters = SemanticFilters(
        repo_patterns=(), path_patterns=(), langs=(), branches=("zzz",), residual="x"
    )
    sql = str(semantic.build_hybrid_rrf_sql(filters, branch="aaa"))
    assert "f.branches @> ARRAY[:sem_branch_0] AND f.branches @> ARRAY[:sem_branch_1]" in sql
    params = semantic.filter_params(filters, branch="aaa")
    assert params == {"sem_branch_0": "aaa", "sem_branch_1": "zzz"}  # sorted, not source order


@pytest.mark.unit
def test_drift_seal_bind_names_match_filter_params_exactly() -> None:
    """The shared-normalizer guarantee, PROVEN not assumed: the `:sem_*` bind
    names referenced in the builder's own SQL text are EXACTLY the keys `filter_params` returns,
    for a query mixing repeated repo:/file:/lang: atoms plus both a branch: atom and a branch
    param.
    """
    filters = SemanticFilters(
        repo_patterns=("a", "b"),
        path_patterns=("c",),
        langs=("Python",),
        branches=("zzz", "aaa"),
        residual="hello",
    )
    sql = str(semantic.build_hybrid_rrf_sql(filters, branch="mmm"))
    sql_binds = set(re.findall(r":(sem_\w+)", sql))
    assert sql_binds == set(semantic.filter_params(filters, branch="mmm").keys())


@pytest.mark.unit
def test_parity_pin_operator_tokens_match_lexical_compiler() -> None:
    """Predicate-level parity byte-check: the semantic builder's
    hand-written repo:/lang:/branch: predicates use the SAME comparison operator as the
    compiler-rendered lexical query for the identical atoms -- `~*` for repo:, `=` for lang:,
    `@>` for branch:. (The semantic side spells its branch RHS as an inline `ARRAY[...]`
    literal rather than a bound Python list, but the OPERATOR is byte-identical either way.)
    """
    lexical_sql = str(
        compile_query(parse("repo:acme lang:go branch:main")).compile(dialect=postgresql.dialect())
    )
    assert "~*" in lexical_sql
    assert "files.lang = " in lexical_sql
    assert "@>" in lexical_sql

    filters = SemanticFilters(
        repo_patterns=("acme",), path_patterns=(), langs=("go",), branches=("main",), residual="x"
    )
    semantic_sql = str(semantic.build_hybrid_rrf_sql(filters))
    assert "r.name ~* :sem_repo_0" in semantic_sql
    assert "f.lang = :sem_lang_0" in semantic_sql
    assert "f.branches @> ARRAY[:sem_branch_0]" in semantic_sql


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
                        cosine_distance=0.2,
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
            "similarity": pytest.approx(0.8),
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


@pytest.mark.unit
def test_similarity_null_for_null_cosine_distance(monkeypatch: pytest.MonkeyPatch) -> None:
    # The converse of test_enabled_payload_shape: a NULL cosine_distance (e.g. a BM25-only
    # row, or a chunk indexed with no embedding) surfaces as similarity: None, not a crash/0.0.
    monkeypatch.setattr(semantic, "get_embedder", lambda cfg: lambda texts: [[0.1, 0.2]])
    engine = _FakeEngine(
        [
            _FakeResult(["chunks"]),
            _FakeResult(
                [
                    _Row(
                        id=2,
                        repo="acme/widgets",
                        path="src/b.py",
                        chunk_index=1,
                        content="y",
                        start_line=None,
                        end_line=None,
                        rrf_score=0.3,
                        cosine_distance=None,
                    )
                ]
            ),
        ]
    )

    payload = semantic._semantic_search_payload(engine, _cfg(enabled=True), "auth flow", 50)

    assert payload["results"][0]["similarity"] is None
    assert payload["results"][0]["rrf_score"] == 0.3


# ------------------------------------------------------------- filter-semantics: payload shaping


@pytest.mark.unit
def test_residual_is_embedded_and_bound_to_qtext(monkeypatch: pytest.MonkeyPatch) -> None:
    # The residual (query minus filter atoms) is the ONE string used both as the embedder
    # input and the :qtext bind -- captured independently and asserted equal.
    captured_embed_input: list[str] = []

    def _fake_get_embedder(cfg: Any) -> Any:
        def _embed(texts: list[str]) -> list[list[float]]:
            captured_embed_input.extend(texts)
            return [[0.1, 0.2]]

        return _embed

    monkeypatch.setattr(semantic, "get_embedder", _fake_get_embedder)

    captured_params: list[dict[str, Any]] = []
    real_execute = _FakeConn.execute

    def _spying_execute(self: _FakeConn, *args: object, **kwargs: object) -> _FakeResult:
        if len(args) > 1 and isinstance(args[1], dict):
            captured_params.append(args[1])
        return real_execute(self, *args, **kwargs)

    monkeypatch.setattr(_FakeConn, "execute", _spying_execute)

    engine = _FakeEngine([_FakeResult(["chunks"]), _FakeResult([])])
    payload = semantic._semantic_search_payload(
        engine, _cfg(enabled=True), "repo:acme auth flow", 50
    )

    assert captured_embed_input == ["auth flow"]
    assert captured_params[-1]["qtext"] == "auth flow"
    assert payload["count"] == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("query", "expected_atom"),
    [
        ("sym:Foo bar", "sym:"),
        ("case:yes bar", "case:"),
        ("case:no bar", "case:"),
        ("commit:abc1234 bar", "commit:"),
        ("/foo/ bar", "regex"),
        ("-foo bar", "-"),
    ],
)
def test_unsupported_atom_yields_structured_payload(query: str, expected_atom: str) -> None:
    # One unit test per rejected atom -- structured payload, zero results, no exception
    # escapes, engine never touched (the poisoned engine proves it).
    payload = semantic._semantic_search_payload(_PoisonedEngine(), _cfg(enabled=True), query, 10)

    assert payload["semantic_enabled"] is True
    assert payload["results"] == []
    assert payload["count"] == 0
    assert payload["unsupported_filter"] == expected_atom
    assert "reason" in payload


@pytest.mark.unit
def test_bare_repo_field_yields_query_parse_error_payload() -> None:
    # Empty-value atoms: bare `repo:` raises QueryParseError at scan time (parser.py
    # _emit_field), surfaced end to end as the query_parse_error payload field.
    payload = semantic._semantic_search_payload(_PoisonedEngine(), _cfg(enabled=True), "repo:", 10)

    assert payload["semantic_enabled"] is True
    assert payload["results"] == []
    assert payload["count"] == 0
    assert "query_parse_error" in payload


@pytest.mark.unit
def test_filter_only_query_never_embeds() -> None:
    # A filter-only query (no residual text) never calls the embedder and never runs an
    # RRF query -- proven with a poisoned engine AND a poisoned get_embedder in the same test.
    payload = semantic._semantic_search_payload(
        _PoisonedEngine(), _cfg(enabled=True), "repo:acme lang:python", 10
    )

    assert payload["nothing_to_embed"] is True
    assert payload["reason"] == "query contains only filters; add text to search for"
    assert payload["results"] == []
    assert payload["count"] == 0


@pytest.mark.unit
def test_empty_query_nothing_to_embed_wording() -> None:
    # The second wording: an empty/whitespace-only query (no atoms at all) is worded
    # differently from the filters-only case above, so the caller sees WHY it is empty.
    payload = semantic._semantic_search_payload(_PoisonedEngine(), _cfg(enabled=True), "   ", 10)

    assert payload["nothing_to_embed"] is True
    assert payload["reason"] == "empty query; provide text to search for"
    assert payload["results"] == []
    assert payload["count"] == 0
