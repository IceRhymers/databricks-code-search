"""Hybrid semantic + BM25 search over ``chunks`` via reciprocal-rank fusion (issue #14).

The serve-side companion to :mod:`app.search.grep` / :mod:`app.search.symbols` for
natural-language queries. Where grep answers *which lines match a zoekt pattern*, this
module answers *which chunks are most relevant to a free-text query*, fusing a vector-ANN
leg (cosine distance over ``chunks.embedding``) and a BM25 leg (over the generated
``chunks.ts`` ``tsvector``) with reciprocal-rank fusion (RRF, ``k=60``).

Two load-bearing shapes, both grounded against the live Lakebase project (plan REV 3):

1. **One shared RRF fusion wrapper, two rank-CTE legs.** The fusion arithmetic (two
   rank CTEs, ``FULL OUTER JOIN``, ``1/(k+rank)`` sum) wraps a per-leg distance/score
   fragment (:data:`_ANN_METRIC` / :data:`_BM_METRIC`). Each leg's ``ORDER BY <metric>
   LIMIT :topk`` runs in an INNER subquery so the ANN / BM25 index is usable (an
   index-defeating outer ``row_number()`` sort is the thing to avoid); ``row_number()``
   then ranks only those ``:topk`` rows. The legs use the real ``lakebase_ann`` ``<=>``
   and ``lakebase_bm25`` ``<@>`` / ``to_bm25query`` operators -- this project is
   Lakebase-only, so there is no other backend. The BM25 fragment is shaped so
   ``ORDER BY metric ASC`` puts the best row first (lakebase BM25 scores are negative;
   more-negative = better).

2. **The query vector is a bound param, never interpolated.** :func:`format_vector_literal`
   builds ``"[f0,f1,...]"`` with ``repr`` (``repr(1e-05) == "1e-05"``; ``format(x, "r")`` is
   an invalid format code and raises), and it is bound as ``:qvec`` cast ``(:qvec)::vector``
   -- no ``register_vector`` adapter, no f-string interpolation of floats into SQL.

Result envelope: each result returns ``chunk_index`` + ``content`` (joined ``chunks ->
files -> repos`` for the ``repo`` name and file ``path``) plus the chunk's 1-based
inclusive ``start_line``/``end_line`` (issue #44) -- nullable, NULL for rows indexed
before the line-aware chunk writer.

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

# lakebase_ann `<=>` cosine distance, ASC = nearer. Both metrics are qualified `c.`
# (0003, Option D1): the inner subquery's FROM joins chunks/files/repos for branch
# scoping, and qualification there is load-bearing -- see _leg_cte.
_ANN_METRIC = "c.embedding <=> (:qvec)::vector"

# BM25 score fragment, shaped so ORDER BY metric ASC ranks best-first: lakebase_bm25
# `<@>`/to_bm25query scores are negative (more-negative = better).
_BM_METRIC = "c.ts <@> to_bm25query(to_tsvector('english', :qtext), 'ix_chunks_ts_bm25'::regclass)"


# ----------------------------------------------------------------------- pure helpers


def format_vector_literal(vec: list[float]) -> str:
    """Render ``vec`` as a pgvector text literal ``"[f0,f1,...]"`` for the ``:qvec`` bind.

    Uses ``repr(x)`` (NOT ``format(x, "r")`` -- ``"r"`` is not a valid float format code and
    raises ``ValueError``). ``repr`` round-trips every float, including scientific notation
    (``repr(1e-05) == "1e-05"``), which pgvector's input parser accepts. The result is bound
    as a parameter and cast ``(:qvec)::vector`` -- it is NEVER interpolated into the SQL text.
    """
    return "[" + ",".join(repr(x) for x in vec) + "]"


def _branch_predicate(branch: str | None) -> str:
    """The branch-scoping WHERE fragment shared by both legs' inner subquery (0003, Option D1).

    ``branch=None`` (default-branch): a correlated match against each chunk's own repo --
    ``coalesce(r.default_branch, 'HEAD') = ANY(f.branches)`` -- byte-identical to the query
    compiler's implicit default conjunct and the ``0003`` backfill/``get_file`` sites (a NULL
    ``default_branch`` resolves to ``'HEAD'`` everywhere). Explicit ``branch``: the GIN-served
    exact-membership operator, ``f.branches @> ARRAY[:branch]``.
    """
    if branch is None:
        return "coalesce(r.default_branch, 'HEAD') = ANY(f.branches)"
    return "f.branches @> ARRAY[:branch]"


def _leg_cte(name: str, metric: str, branch_pred: str, *, extra_where: str = "") -> str:
    """One rank CTE: rank the top ``:topk`` rows by ``metric`` (ASC = best) as 1..topk.

    The ``ORDER BY metric LIMIT :topk`` lives in an INNER subquery so the ANN / BM25 index is
    usable; ``row_number()`` then ranks only those candidates (never a full-table sort).
    ``branch_pred`` (see :func:`_branch_predicate`) is always present -- it lives in the INNER
    subquery's ``WHERE``, never the ``ORDER BY``, so it never costs the index a second sort key.

    Qualification scope is critical (0003): the INNER subquery joins ``chunks c`` to
    ``files f`` / ``repos r`` for branch scoping, so ``metric``/``extra_where`` must be
    ``c.``-qualified (``c.embedding``, ``c.ts``) and the inner projection is ``SELECT c.id AS
    id``. The OUTER ``row_number()`` window (this CTE's own ``SELECT``) stays BARE -- ``id``,
    never ``c.id`` -- because it selects from the derived table ``s``, whose only columns are
    ``id``/``metric``; ``c`` is out of scope there and referencing it raises "missing
    FROM-clause entry for table c".

    Determinism (issues #9/#13) is enforced ONLY where it does not cost the index:

    * ``row_number()`` breaks ties on ``id``. Scores plateau (BM25 especially), and arbitrary
      rank assignment among equals changes each row's ``1/(k+rank)`` contribution, so the same
      query over the same corpus could otherwise fuse to a different order. This is free: the
      window sorts an already-materialized <= topk-row set, no index involved.
    * The INNER ``ORDER BY`` stays a SINGLE expression on BOTH legs. This is load-bearing:
      Postgres cannot build an ordered-index path when a second sort key follows an
      ``ORDER BY``-operator key, and the fallback is not cost-based -- it will seq-scan and
      full-sort the table even with ``enable_seqscan = off``. Both ``lakebase_ann``'s ``<=>``
      and ``lakebase_bm25``'s ``<@>`` are such operators, so adding ``, id`` here would
      silently collapse the leg to a full scan in production. It would also buy nothing:
      an approximate index's candidate-set membership is nondeterministic regardless, so
      there is no tie-stability to win at that level.

    NOTE: the unit test asserts the SQL SHAPE (no inner tiebreak) rather than a plan --
    plan-level regressions on the lakebase operators are only observable against a real
    Lakebase branch.
    """
    where = f" WHERE {branch_pred}"
    if extra_where:
        where += f" AND {extra_where}"
    return (
        f"{name} AS ("
        f"SELECT id, row_number() OVER (ORDER BY metric, id) AS rank "
        f"FROM (SELECT c.id AS id, {metric} AS metric "
        f"FROM chunks c JOIN files f ON f.id = c.file_id JOIN repos r ON r.id = f.repo_id"
        f"{where} "
        f"ORDER BY {metric} LIMIT :topk) s)"
    )


def build_hybrid_rrf_sql(branch: str | None = None) -> TextClause:
    """Build the hybrid RRF query as a parameterized :class:`TextClause`.

    Binds ``:qvec`` (bracketed vector literal), ``:qtext`` (raw query string), ``:topk``
    (per-leg candidate cap), ``:k`` (RRF constant), ``:lim`` (result cap), and -- only when
    ``branch`` is given -- ``:branch`` (exact branch name; see :func:`_branch_predicate`).
    Rows come back as ``(id, repo, path, chunk_index, content, start_line, end_line,
    rrf_score)`` after joining the fused ids back to ``chunks -> files -> repos``.
    """
    branch_pred = _branch_predicate(branch)
    # ANN leg skips NULL embeddings: `embedding <=> :qvec` is NULL for them and Postgres
    # sorts NULLs LAST in ASC, so they stay hidden until the corpus is smaller than :topk --
    # at which point they would take real ranks and earn real RRF credit for not matching.
    ann = _leg_cte("ann", _ANN_METRIC, branch_pred, extra_where="c.embedding IS NOT NULL")
    # No inner tiebreak on this leg either -- see _leg_cte: a second sort key after the
    # `<@>` ORDER BY-operator key would make the lakebase_bm25 index path unavailable.
    bm = _leg_cte("bm", _BM_METRIC, branch_pred)
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
        "c.chunk_index AS chunk_index, c.content AS content, "
        "c.start_line AS start_line, c.end_line AS end_line, fused.rrf AS rrf_score "
        "FROM fused "
        "JOIN chunks c ON c.id = fused.id "
        "JOIN files f ON f.id = c.file_id "
        "JOIN repos r ON r.id = f.repo_id "
        "ORDER BY fused.rrf DESC, fused.id"
    )
    return text(sql)


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
        "reason": "semantic search is explicitly disabled "
        "(remove CODE_SEARCH_SEMANTIC_ENABLED=0 to re-enable)",
    }


def _semantic_not_migrated_payload(query: str) -> dict[str, Any]:
    """The flag is on but the ``chunks`` migration has not run -- recoverable, not a fault.

    Reachable on a deployment whose core migrations predate ``0004`` (the next
    ``make migrate`` / ``make deploy`` creates ``chunks``). A missing migration is an
    operator-fixable condition in the same class as ``query_too_broad``, so it surfaces
    as a payload field rather than an exception -- see ``app/main.py``'s dispatch
    contract.
    """
    return {
        "query": query,
        "semantic_enabled": True,
        "results": [],
        "count": 0,
        "semantic_schema_missing": True,
        "reason": "semantic schema not present; run `make migrate` or redeploy (see "
        "docs/runbooks/semantic-enablement.md)",
    }


def _semantic_search_payload(
    engine: Engine, cfg: Settings, query: str, limit: int, branch: str | None = None
) -> dict[str, Any]:
    """Embed ``query``, run the hybrid RRF search, and shape the ranked-chunk envelope.

    FIRST line short-circuits to :func:`_semantic_disabled_payload` when the feature is
    explicitly disabled (``CODE_SEARCH_SEMANTIC_ENABLED=0``) -- BEFORE the engine or the
    embedder is touched, so the disabled path never opens a connection or imports
    ``databricks-sdk`` (plan P2/A1). Only when enabled does it lazily build the embedder,
    embed the query text OUTSIDE the DB transaction (no network call inside
    ``conn.begin()``), then run the RRF query under a transaction-local
    ``statement_timeout`` and join the fused ids back to ``chunks -> files -> repos``.

    ``branch`` (0003, Option D1): ``None`` scopes each leg to its chunk's own repo's default
    branch (the same ``coalesce(...,'HEAD')`` as the query compiler); given, it scopes to an
    exact ``branch:`` match instead. Threaded straight to :func:`build_hybrid_rrf_sql` -- this
    module takes natural-language queries, not zoekt grammar, so branch scoping is a separate
    parameter rather than a ``branch:`` atom.

    Result envelope: ``{"query", "semantic_enabled": True, "results": [{"repo", "file",
    "chunk_index", "content", "start_line", "end_line", "rrf_score"}], "count"}``.
    ``start_line``/``end_line`` are 1-based inclusive (issue #44) and ``None`` for rows
    indexed before the line-aware chunk writer (consumers fall back to needle-matching).
    """
    if not cfg.semantic_enabled:
        return _semantic_disabled_payload(query)

    # Schema probe FIRST, in its own short transaction: if the 0004 migration has not
    # run there is nothing to search, and discovering that should not cost a paid embedding
    # call. Kept separate from the query transaction so the embed below still happens with no
    # transaction open (no lock held across a network call).
    with engine.connect() as conn:
        with conn.begin():
            # SET LOCAL (int-coerced -> injection-safe) is transaction-scoped, matching the
            # other builders; it never leaks a statement_timeout onto the pooled connection.
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            chunks_present = conn.execute(text("SELECT to_regclass('chunks')")).scalar() is not None
    if not chunks_present:
        return _semantic_not_migrated_payload(query)

    # Embed OUTSIDE the connection/transaction: no network call inside the lock window.
    qvec = get_embedder(cfg)([query])[0]
    params: dict[str, Any] = {
        "qvec": format_vector_literal(qvec),
        "qtext": query,
        "topk": SEMANTIC_TOP_K,
        "k": SEMANTIC_RRF_K,
        "lim": limit,
    }
    if branch is not None:
        params["branch"] = branch

    with engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            rows = conn.execute(build_hybrid_rrf_sql(branch), params).all()

    results = [
        {
            "repo": row.repo,
            "file": row.path,
            "chunk_index": row.chunk_index,
            "content": row.content,
            # 1-based inclusive line range (issue #44); None for rows indexed before the
            # line-aware writer (consumers fall back to needle-matching).
            "start_line": row.start_line,
            "end_line": row.end_line,
            "rrf_score": float(row.rrf_score),
        }
        for row in rows
    ]
    return {
        "query": query,
        "semantic_enabled": True,
        "results": results,
        "count": len(results),
    }
