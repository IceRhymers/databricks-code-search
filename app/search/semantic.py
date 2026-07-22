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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.sql.elements import TextClause

from app.config import Settings
from app.query.parser import QueryParseError
from app.query.semantic_filters import (
    SemanticFilters,
    UnsupportedSemanticAtomError,
    split_semantic_query,
)

if TYPE_CHECKING:
    from app.embed import EmbedFn

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


def _branch_predicate(branches: tuple[str, ...]) -> str:
    """The branch-scoping WHERE fragment shared by both legs' inner subquery (0003, Option D1).

    ``branches == ()`` (default-branch, no atom and no ``branch`` param): a correlated match
    against each chunk's own repo -- ``coalesce(r.default_branch, 'HEAD') = ANY(f.branches)`` --
    byte-identical to the query compiler's implicit default conjunct and the ``0003``
    backfill/``get_file`` sites (a NULL ``default_branch`` resolves to ``'HEAD'`` everywhere).
    One or more explicit branch values (Decision C1, filter-semantics): the GIN-served
    exact-membership operator, AND-composed once per value -- ``f.branches @> ARRAY[:sem_branch_0]
    AND f.branches @> ARRAY[:sem_branch_1] ...`` -- conjunctive, the same semantics as lexical
    ``branch:a branch:b`` (mirrors ``app.query.compiler``'s explicit-``branch:`` path).
    """
    if not branches:
        return "coalesce(r.default_branch, 'HEAD') = ANY(f.branches)"
    return " AND ".join(f"f.branches @> ARRAY[:sem_branch_{i}]" for i in range(len(branches)))


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


@dataclass(frozen=True)
class _CompiledFilters:
    """The single derivation both :func:`build_hybrid_rrf_sql` and :func:`filter_params` read
    from -- so the predicates the builder emits and the binds the params companion returns can
    never drift apart (Decision C1's "shared normalizer" guarantee).

    ``repo_file_lang_where``: the AND-joined ``repo:``/``file:``/``lang:`` predicate fragment
    (``""`` when none of those filters are present). ``branch_pred``: see
    :func:`_branch_predicate`. ``binds``: every ``sem_*`` parameter name -> value referenced by
    either fragment above.
    """

    repo_file_lang_where: str
    branch_pred: str
    binds: dict[str, str]


def _normalized_filter_state(
    filters: SemanticFilters | None, branch: str | None
) -> _CompiledFilters:
    """Normalize ``filters`` + the legacy ``branch`` kwarg into predicates + binds, ONCE.

    Branch unification (Decision C1, iteration 3 MAJOR): the normalized branch list is
    ``sorted(set(atom_values) | ({branch} if branch else set()))`` -- a param equal to an
    existing ``branch:`` atom dedupes to one predicate (it is the same set element); a
    param-only call (``filters=None``) still routes through this list, so ``branch="x"`` and a
    bare ``branch:x`` atom emit byte-identical SQL. Any branch value present (atom or param)
    suppresses the implicit default-branch coalesce arm. ``lang:`` values are normalized
    (``.strip().lower()``) here, matching ``app.query.compiler``'s ``_lower`` byte-for-byte
    (KD-3); ``repo:``/``file:`` values stay opaque (bound raw, matched as regexes downstream).
    """
    repo_patterns = filters.repo_patterns if filters is not None else ()
    path_patterns = filters.path_patterns if filters is not None else ()
    langs = tuple(v.strip().lower() for v in (filters.langs if filters is not None else ()))
    atom_branches = filters.branches if filters is not None else ()
    branches = tuple(sorted(set(atom_branches) | ({branch} if branch else set())))

    clauses: list[str] = []
    binds: dict[str, str] = {}
    for i, pattern in enumerate(repo_patterns):
        name = f"sem_repo_{i}"
        clauses.append(f"r.name ~* :{name}")
        binds[name] = pattern
    for i, pattern in enumerate(path_patterns):
        name = f"sem_file_{i}"
        clauses.append(f"f.path ~* :{name}")
        binds[name] = pattern
    for i, lang in enumerate(langs):
        name = f"sem_lang_{i}"
        clauses.append(f"f.lang = :{name}")
        binds[name] = lang

    branch_pred = _branch_predicate(branches)
    for i, value in enumerate(branches):
        binds[f"sem_branch_{i}"] = value

    return _CompiledFilters(
        repo_file_lang_where=" AND ".join(clauses),
        branch_pred=branch_pred,
        binds=binds,
    )


def filter_params(
    filters: SemanticFilters | None = None, branch: str | None = None
) -> dict[str, str]:
    """Return the ``sem_*`` bind dict for ``filters``/``branch`` -- the companion to
    :func:`build_hybrid_rrf_sql`. Both derive from :func:`_normalized_filter_state`, so the
    keys returned here always equal the ``:sem_*`` bind names the builder's SQL text references
    (proven by a drift-seal unit test, not just asserted by construction).
    """
    return _normalized_filter_state(filters, branch).binds


def build_hybrid_rrf_sql(
    filters: SemanticFilters | None = None, branch: str | None = None
) -> TextClause:
    """Build the hybrid RRF query as a parameterized :class:`TextClause`.

    Binds ``:qvec`` (bracketed vector literal), ``:qtext`` (residual query text), ``:topk``
    (per-leg candidate cap), ``:k`` (RRF constant), ``:lim`` (result cap), and -- only when a
    ``repo:``/``file:``/``lang:``/``branch:`` filter or the ``branch`` kwarg is present --
    numbered ``:sem_*`` binds (see :func:`filter_params`, which returns the exact same dict).
    ``filters`` (:class:`app.query.semantic_filters.SemanticFilters`, from
    :func:`app.query.semantic_filters.split_semantic_query`) compiles to WHERE predicates
    inside BOTH leg CTEs' inner subqueries -- filter-then-rank, never post-filtered. ``branch``
    is kept for call-site compatibility; its value is folded into the SAME normalized branch
    list as any ``branch:`` atom (Decision C1) -- there is no separate ``:branch`` bind or
    predicate arm. Rows come back as ``(id, repo, path, chunk_index, content, start_line,
    end_line, rrf_score, cosine_distance)`` after joining the fused ids back to
    ``chunks -> files -> repos``.
    """
    compiled = _normalized_filter_state(filters, branch)
    # ANN leg skips NULL embeddings: `embedding <=> :qvec` is NULL for them and Postgres
    # sorts NULLs LAST in ASC, so they stay hidden until the corpus is smaller than :topk --
    # at which point they would take real ranks and earn real RRF credit for not matching.
    ann_extra = " AND ".join(
        part for part in (compiled.repo_file_lang_where, "c.embedding IS NOT NULL") if part
    )
    ann = _leg_cte("ann", _ANN_METRIC, compiled.branch_pred, extra_where=ann_extra)
    # No inner tiebreak on this leg either -- see _leg_cte: a second sort key after the
    # `<@>` ORDER BY-operator key would make the lakebase_bm25 index path unavailable.
    bm = _leg_cte("bm", _BM_METRIC, compiled.branch_pred, extra_where=compiled.repo_file_lang_where)
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
        "c.start_line AS start_line, c.end_line AS end_line, fused.rrf AS rrf_score, "
        # Recomputed here (Decision B1), not carried from the ANN leg: every fused row gets a
        # distance -- including BM25-only rows that never entered the ANN top-:topk (exactly
        # the noise rows callers most need to judge). NULL-embedding rows yield SQL NULL.
        f"{_ANN_METRIC} AS cosine_distance "
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
    (inside :func:`app.embed.databricks_embedder`) and constructs the serving-endpoint
    client; a double-checked ``threading.Lock`` makes a first-build race between two MCP
    sessions safe. Never reached on the flag-off path (the payload builder short-circuits
    first), so importing this module / calling ``semantic_search`` disabled never touches the
    SDK.
    """
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                from app.embed import get_embedder as build_embedder  # lazy: SDK import

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


# Every rejected atom's remedy: WHAT to do instead, so the breaking change on `commit:` (it used
# to be plain prose, see the ADR in .omc/plans/ralplan-semantic-filter-similarity.md) and the
# other rejections are self-documenting at the point of impact, not just a bare error string.
_UNSUPPORTED_FILTER_REMEDIES: dict[str, str] = {
    "commit:": "commit-scoped search is lexical-only; use search_code",
    "sym:": "symbol filters are lexical-only; use search_code",
    "case:": "case sensitivity does not apply to semantic ranking; remove case:",
    "regex": "regex atoms are not supported in semantic queries; quote the term to search it "
    "as text",
    "-": "negation is not supported in semantic queries; remove the leading '-' or quote the "
    "term to search it as text",
}


def _semantic_query_parse_error_payload(query: str, error: QueryParseError) -> dict[str, Any]:
    """A malformed query (unterminated quote/regex, empty field value, bad commit:/case: value)."""
    return {
        "query": query,
        "semantic_enabled": True,
        "results": [],
        "count": 0,
        "query_parse_error": str(error),
    }


def _semantic_unsupported_filter_payload(
    query: str, error: UnsupportedSemanticAtomError
) -> dict[str, Any]:
    """`sym:`/`case:`/`commit:`/regex/negation atom: a loud, remedy-bearing rejection, not prose."""
    return {
        "query": query,
        "semantic_enabled": True,
        "results": [],
        "count": 0,
        "unsupported_filter": error.atom,
        "reason": _UNSUPPORTED_FILTER_REMEDIES[error.atom],
    }


def _semantic_nothing_to_embed_payload(query: str, *, has_filters: bool) -> dict[str, Any]:
    """Empty residual: no embedding call is made. Wording branches on WHY it is empty --
    filters consumed everything vs. there was never any text -- so the caller sees why."""
    reason = (
        "query contains only filters; add text to search for"
        if has_filters
        else "empty query; provide text to search for"
    )
    return {
        "query": query,
        "semantic_enabled": True,
        "results": [],
        "count": 0,
        "nothing_to_embed": True,
        "reason": reason,
    }


def _semantic_search_payload(
    engine: Engine, cfg: Settings, query: str, limit: int, branch: str | None = None
) -> dict[str, Any]:
    """Split filters out of ``query``, embed the residual, run the hybrid RRF search, and shape
    the ranked-chunk envelope.

    FIRST line short-circuits to :func:`_semantic_disabled_payload` when the feature is
    explicitly disabled (``CODE_SEARCH_SEMANTIC_ENABLED=0``) -- BEFORE the engine or the
    embedder is touched, so the disabled path never opens a connection or imports
    ``databricks-sdk`` (plan P2/A1) and never even parses ``query``.

    When enabled, :func:`app.query.semantic_filters.split_semantic_query` runs next -- a pure,
    DB-free step -- and any of its three recoverable outcomes return BEFORE the schema probe or
    the embedder, so none of them cost a DB round-trip or an embedding call: a malformed query
    (``query_parse_error``), an unsupported atom (``sym:``/``case:``/``commit:``/regex --
    ``unsupported_filter`` + a remedy in ``reason``), or an empty residual after filters are
    stripped (``nothing_to_embed``, worded differently for filters-only vs. an empty/whitespace
    query). Only past all three does it probe the schema, lazily build the embedder, embed the
    RESIDUAL text (not the raw query) OUTSIDE the DB transaction (no network call inside
    ``conn.begin()``), then run the RRF query under a transaction-local ``statement_timeout``
    and join the fused ids back to ``chunks -> files -> repos``.

    ``branch`` (0003, Option D1; unified with in-query ``branch:`` atoms by Decision C1,
    filter-semantics): sugar for a ``branch:`` atom, conjunctive with any already in ``query``.
    No value anywhere scopes each leg to its chunk's own repo's default branch (the same
    ``coalesce(...,'HEAD')`` as the query compiler); threaded straight to
    :func:`build_hybrid_rrf_sql` / :func:`filter_params`.

    Result envelope: ``{"query", "semantic_enabled": True, "results": [{"repo", "file",
    "chunk_index", "content", "start_line", "end_line", "rrf_score", "similarity"}], "count"}``.
    ``start_line``/``end_line`` are 1-based inclusive (issue #44) and ``None`` for rows
    indexed before the line-aware chunk writer (consumers fall back to needle-matching).
    ``similarity`` is ``1 - cosine_distance`` (the ANN metric, recomputed per result -- Decision
    B1), ``None`` for rows whose chunk has no embedding.
    """
    if not cfg.semantic_enabled:
        return _semantic_disabled_payload(query)

    try:
        filters = split_semantic_query(query)
    except QueryParseError as error:
        return _semantic_query_parse_error_payload(query, error)
    except UnsupportedSemanticAtomError as error:
        return _semantic_unsupported_filter_payload(query, error)

    if not filters.residual:
        has_filters = bool(
            filters.repo_patterns or filters.path_patterns or filters.langs or filters.branches
        )
        return _semantic_nothing_to_embed_payload(query, has_filters=has_filters)

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

    # Embed the RESIDUAL text (filters stripped), OUTSIDE the connection/transaction: no
    # network call inside the lock window.
    qvec = get_embedder(cfg)([filters.residual])[0]
    params: dict[str, Any] = {
        "qvec": format_vector_literal(qvec),
        "qtext": filters.residual,
        "topk": SEMANTIC_TOP_K,
        "k": SEMANTIC_RRF_K,
        "lim": limit,
    }
    params.update(filter_params(filters, branch))

    with engine.connect() as conn:
        with conn.begin():
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(cfg.statement_timeout_ms)}")
            rows = conn.execute(build_hybrid_rrf_sql(filters, branch), params).all()

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
            # Raw cosine similarity (Decision B1): uninterpretable fused rrf_score alone hides
            # true-match/noise separation; NULL cosine_distance (no embedding) -> None.
            "similarity": (1.0 - row.cosine_distance) if row.cosine_distance is not None else None,
        }
        for row in rows
    ]
    return {
        "query": query,
        "semantic_enabled": True,
        "results": results,
        "count": len(results),
    }
