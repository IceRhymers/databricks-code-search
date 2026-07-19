"""Hybrid semantic + BM25 search over ``chunks`` via reciprocal-rank fusion (issue #14).

The serve-side companion to :mod:`app.search.grep` / :mod:`app.search.symbols` for
natural-language queries. Where grep answers *which lines match a zoekt pattern*, this
module answers *which chunks are most relevant to a free-text query*, fusing a vector-ANN
leg (cosine distance over ``chunks.embedding``) and a BM25 leg (over the generated
``chunks.ts`` ``tsvector``) with reciprocal-rank fusion (RRF, ``k=60``).

Two load-bearing shapes, both grounded against the live Lakebase project (plan REV 3):

1. **One shared RRF fusion wrapper, backend-selected legs.** The fusion arithmetic (two
   rank CTEs, ``FULL OUTER JOIN``, ``1/(k+rank)`` sum) is IDENTICAL for both backends; only
   the per-leg distance/score fragment differs (:data:`_ANN_METRIC` / :data:`_BM_METRIC`).
   Each leg's ``ORDER BY <metric> LIMIT :topk`` runs in an INNER subquery so the ANN / BM25
   index is usable (an index-defeating outer ``row_number()`` sort is the thing to avoid);
   ``row_number()`` then ranks only those ``:topk`` rows. The ``lakebase`` backend uses the
   real ``lakebase_ann`` ``<=>`` and ``lakebase_bm25`` ``<@>`` / ``to_bm25query`` operators;
   the ``standin`` backend (CI ``pgvector/pgvector:pg16``, which lacks the ``lakebase_*``
   extensions) uses pgvector's identical ``<=>`` and substitutes ``ts_rank_cd`` for BM25 --
   an APPROXIMATION that exercises the fusion plumbing and the real ANN operator but NOT the
   production BM25 ranking (that is proven only by the live smoke leg). Both BM25 fragments
   are shaped so ``ORDER BY metric ASC`` puts the best row first (lakebase BM25 scores are
   negative; the standin negates ``ts_rank_cd``), keeping the wrapper byte-identical.

2. **The query vector is a bound param, never interpolated.** :func:`format_vector_literal`
   builds ``"[f0,f1,...]"`` with ``repr`` (``repr(1e-05) == "1e-05"``; ``format(x, "r")`` is
   an invalid format code and raises), and it is bound as ``:qvec`` cast ``(:qvec)::vector``
   -- no ``register_vector`` adapter, no f-string interpolation of floats into SQL.

Result envelope (V1 limitation, documented): ``chunks`` carries no line ranges, so each
result returns ``chunk_index`` + ``content`` (joined ``chunks -> files -> repos`` for the
``repo`` name and file ``path``), not a precise ``start_line``/``end_line``.

Flag-off is a true no-op: :func:`_semantic_search_payload` short-circuits on the FIRST line
when ``cfg.semantic_enabled`` is false, returning the feature-absent payload BEFORE touching
the engine or the embedder -- so ``databricks-sdk`` is never imported on the disabled path.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.sql.elements import TextClause

from app.config import Settings

if TYPE_CHECKING:
    from indexer.embed import EmbedFn

# RRF and candidate-set defaults (plan REV 3): k=60 dampens the head of each rank list; each
# leg contributes its top :topk index-accelerated candidates before fusion.
SEMANTIC_RRF_K = 60
SEMANTIC_TOP_K = 200

# The ANN distance fragment is identical on both backends (pgvector `<=>` == lakebase_ann
# `<=>`, cosine distance, ASC = nearer). Only the BM25 fragment differs.
_ANN_METRIC = "embedding <=> (:qvec)::vector"

# Per-backend BM25 score fragment, each shaped so ORDER BY metric ASC ranks best-first:
# lakebase_bm25 `<@>`/to_bm25query scores are negative (more-negative = better); the pgvector
# stand-in negates ts_rank_cd (higher relevance = better) so the wrapper's ORDER BY is shared.
_BM_METRIC = {
    "lakebase": (
        "ts <@> to_bm25query(to_tsvector('english', :qtext), 'ix_chunks_ts_bm25'::regclass)"
    ),
    "standin": "- ts_rank_cd(ts, plainto_tsquery('english', :qtext))",
}


# ----------------------------------------------------------------------- pure helpers


def format_vector_literal(vec: list[float]) -> str:
    """Render ``vec`` as a pgvector text literal ``"[f0,f1,...]"`` for the ``:qvec`` bind.

    Uses ``repr(x)`` (NOT ``format(x, "r")`` -- ``"r"`` is not a valid float format code and
    raises ``ValueError``). ``repr`` round-trips every float, including scientific notation
    (``repr(1e-05) == "1e-05"``), which pgvector's input parser accepts. The result is bound
    as a parameter and cast ``(:qvec)::vector`` -- it is NEVER interpolated into the SQL text.
    """
    return "[" + ",".join(repr(x) for x in vec) + "]"


def _leg_cte(name: str, metric: str, *, where: str = "", tiebreak_inner: bool = False) -> str:
    """One rank CTE: rank the top ``:topk`` rows by ``metric`` (ASC = best) as 1..topk.

    The ``ORDER BY metric LIMIT :topk`` lives in an INNER subquery so the ANN / BM25 index is
    usable; ``row_number()`` then ranks only those candidates (never a full-table sort).

    Determinism (issues #9/#13) applies at BOTH levels, but asymmetrically:

    * ``row_number()`` ALWAYS breaks ties on ``id``. Scores plateau (BM25 especially), and
      arbitrary rank assignment among equals changes each row's ``1/(k+rank)`` contribution --
      so the same query over the same corpus could otherwise fuse to a different order. This
      is free: the window sorts an already-materialized <= topk-row set, no index involved.
    * The INNER ``ORDER BY`` adds ``, id`` only when ``tiebreak_inner`` is set (the BM25 leg).
      It is deliberately NOT added on the ANN leg: a second sort expression there would defeat
      the ``lakebase_ann`` / pgvector index ordering, and an approximate index's candidate-set
      membership is inherently nondeterministic anyway, so the cost buys nothing.
    """
    inner_order = f"{metric}, id" if tiebreak_inner else metric
    return (
        f"{name} AS ("
        f"SELECT id, row_number() OVER (ORDER BY metric, id) AS rank "
        f"FROM (SELECT id, {metric} AS metric FROM chunks{where} "
        f"ORDER BY {inner_order} LIMIT :topk) s)"
    )


def build_hybrid_rrf_sql(backend: str) -> TextClause:
    """Build the backend-selected hybrid RRF query as a parameterized :class:`TextClause`.

    Binds ``:qvec`` (bracketed vector literal), ``:qtext`` (raw query string), ``:topk``
    (per-leg candidate cap), ``:k`` (RRF constant), ``:lim`` (result cap). The shared fusion
    wrapper is identical for ``lakebase`` and ``standin``; only the BM25 leg fragment differs
    (see :data:`_BM_METRIC`). Rows come back as ``(id, repo, path, chunk_index, content,
    rrf_score)`` after joining the fused ids back to ``chunks -> files -> repos``.
    """
    if backend not in _BM_METRIC:
        raise ValueError(f"unknown semantic backend {backend!r}; expected 'lakebase' or 'standin'")
    # ANN leg skips NULL embeddings: `embedding <=> :qvec` is NULL for them and Postgres
    # sorts NULLs LAST in ASC, so they stay hidden until the corpus is smaller than :topk --
    # at which point they would take real ranks and earn real RRF credit for not matching.
    ann = _leg_cte("ann", _ANN_METRIC, where=" WHERE embedding IS NOT NULL")
    # BM25 scores plateau hard, so its candidate SET (not just the ranking) needs a tiebreak.
    bm = _leg_cte("bm", _BM_METRIC[backend], tiebreak_inner=True)
    sql = (
        f"WITH {ann}, {bm}, "
        "fused AS ("
        "SELECT id, "
        "coalesce(1.0 / (:k + ann.rank), 0) + coalesce(1.0 / (:k + bm.rank), 0) AS rrf "
        "FROM ann FULL OUTER JOIN bm USING (id) "
        # `, id` is load-bearing, not decoration: RRF scores tie constantly (any two
        # chunks at the same rank pair sum identically), so without a tiebreak WHICH
        # tied rows survive this LIMIT is unspecified -- the outer ORDER BY would then
        # be deterministically sorting a nondeterministic set. Matches the explicit
        # id-tiebreak determinism convention from issues #9/#13.
        "ORDER BY rrf DESC, id LIMIT :lim) "
        "SELECT fused.id AS id, r.name AS repo, f.path AS path, "
        "c.chunk_index AS chunk_index, c.content AS content, fused.rrf AS rrf_score "
        "FROM fused "
        "JOIN chunks c ON c.id = fused.id "
        "JOIN files f ON f.id = c.file_id "
        "JOIN repos r ON r.id = f.repo_id "
        "ORDER BY fused.rrf DESC, fused.id"
    )
    return text(sql)


def detect_backend(conn: Any) -> str:
    """Return ``'lakebase'`` if the ``lakebase_bm25`` access method exists, else ``'standin'``.

    The BM25 access method is the discriminating capability: it is present only on a
    project whose managed ``shared_preload_libraries`` enabled ``lakebase_text``. CI's
    ``pgvector/pgvector:pg16`` image lacks it, so it selects the stand-in leg.
    """
    row = conn.execute(text("SELECT 1 FROM pg_am WHERE amname = 'lakebase_bm25'")).first()
    return "lakebase" if row is not None else "standin"


# --------------------------------------------------------------- lazy embedder singleton


_embedder: EmbedFn | None = None
_embedder_lock = threading.Lock()


def get_embedder(cfg: Settings) -> EmbedFn:
    """Return the process-scoped query embedder, building it once (lazily, race-safe).

    Mirrors :func:`app.main.get_engine`: the first ENABLED call imports ``databricks-sdk``
    (inside :func:`indexer.embed.databricks_embedder`) and constructs the serving-endpoint
    client; a double-checked ``threading.Lock`` makes a first-build race between two MCP
    sessions safe. Never reached on the flag-off path (the payload builder short-circuits
    first), so importing this module / calling ``semantic_search`` disabled never touches the
    SDK.
    """
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                from indexer.embed import get_embedder as build_embedder  # lazy: SDK import

                _embedder = build_embedder(cfg)
    return _embedder


# ------------------------------------------------------------------------ payload builder


def _semantic_disabled_payload(query: str) -> dict[str, Any]:
    """The feature-absent payload: a clean no-op result, never a 500/503 (plan P2)."""
    return {
        "query": query,
        "semantic_enabled": False,
        "results": [],
        "count": 0,
        "reason": "semantic search is disabled (set CODE_SEARCH_SEMANTIC_ENABLED=1 to enable)",
    }


def _semantic_not_migrated_payload(query: str, backend: str) -> dict[str, Any]:
    """The flag is on but the gated migration has not run -- recoverable, not a fault.

    ``detect_backend`` probes ``pg_am``, which says whether the BETA EXTENSIONS are loaded --
    it cannot say whether ``chunks`` exists. Both orderings are reachable (an operator can set
    the flag before running ``make migrate-semantic``, and the runbook's step list does not
    forbid it), and in both the query would hit ``UndefinedTable``. A missing migration is an
    operator-fixable condition in the same class as ``query_too_broad``, so it surfaces as a
    payload field rather than an exception -- see ``app/main.py``'s dispatch contract.
    """
    return {
        "query": query,
        "semantic_enabled": True,
        "backend": backend,
        "results": [],
        "count": 0,
        "semantic_schema_missing": True,
        "reason": "semantic schema not present; run `make migrate-semantic` (see "
        "docs/runbooks/semantic-enablement.md)",
    }


def _semantic_search_payload(
    engine: Engine, cfg: Settings, query: str, limit: int
) -> dict[str, Any]:
    """Embed ``query``, run the hybrid RRF search, and shape the ranked-chunk envelope.

    FIRST line short-circuits to :func:`_semantic_disabled_payload` when the feature is off --
    BEFORE the engine or the embedder is touched, so the disabled path never opens a
    connection or imports ``databricks-sdk`` (plan P2/A1). Only when enabled does it lazily
    build the embedder, embed the query text OUTSIDE the DB transaction (no network call
    inside ``conn.begin()``), then run the backend-selected RRF query under a transaction-local
    ``statement_timeout`` and join the fused ids back to ``chunks -> files -> repos``.

    Result envelope (V1): ``{"query", "semantic_enabled": True, "backend", "results":
    [{"repo", "file", "chunk_index", "content", "rrf_score"}], "count"}``. ``chunks`` has no
    line ranges in V1, so results carry ``chunk_index`` + ``content`` rather than a precise
    ``start_line``/``end_line`` (documented limitation).
    """
    if not cfg.semantic_enabled:
        return _semantic_disabled_payload(query)

    # Capability probe FIRST, in its own short transaction: if the gated migration has not
    # run there is nothing to search, and discovering that should not cost a paid embedding
    # call. Kept separate from the query transaction so the embed below still happens with no
    # transaction open (no lock held across a network call).
    with engine.connect() as conn:
        with conn.begin():
            # SET LOCAL (int-coerced -> injection-safe) is transaction-scoped, matching the
            # other builders; it never leaks a statement_timeout onto the pooled connection.
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            backend = detect_backend(conn)
            chunks_present = conn.execute(text("SELECT to_regclass('chunks')")).scalar() is not None
    if not chunks_present:
        return _semantic_not_migrated_payload(query, backend)

    # Embed OUTSIDE the connection/transaction: no network call inside the lock window.
    qvec = get_embedder(cfg)([query])[0]
    params = {
        "qvec": format_vector_literal(qvec),
        "qtext": query,
        "topk": SEMANTIC_TOP_K,
        "k": SEMANTIC_RRF_K,
        "lim": limit,
    }

    with engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            rows = conn.execute(build_hybrid_rrf_sql(backend), params).all()

    results = [
        {
            "repo": row.repo,
            "file": row.path,
            "chunk_index": row.chunk_index,
            "content": row.content,
            "rrf_score": float(row.rrf_score),
        }
        for row in rows
    ]
    return {
        "query": query,
        "semantic_enabled": True,
        "backend": backend,
        "results": results,
        "count": len(results),
    }
