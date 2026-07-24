"""Reference resolution: query-time candidate-set resolver over raw ``reference_edges``.

The serve-side companion to :mod:`app.search.symbols` for the knowledge-graph epic (#82).
``reference_edges`` (0005, #83/#84/#85) stores raw, unresolved call/import sites -- deliberately
no FK to ``symbols`` (symbol ids churn on every per-file reindex). This module resolves a raw
edge's ``target_name`` to the ``symbols`` rows it could plausibly mean, at query time, by name.

Design -- two queries, deliberately NOT one joined query (mirrors ``symbols.py``):

1. Edge sites: ``reference_edges JOIN files JOIN repos``, bounded by ``row_limit``.
2. Candidate symbols: ``symbols JOIN files JOIN repos``, filtered to the distinct
   ``target_name``s the first query returned, bounded PER NAME by a SQL window function
   (``candidate_cap``) so a hot name (``get``, ``run``, ``__init__``) never pulls its entire
   corpus-wide match set.

Why two queries and not ``reference_edges JOIN symbols ON target_name = name`` (both reaching
through ``files``/``repos``): that is exactly the self-referencing join shape that lets
SQLAlchemy auto-correlate the two ``files``/``repos`` legs against each other, silently
mis-scoping which candidate belongs to which site. Neither statement here references the
other's tables, so there is zero correlation surface -- the same rationale as
``symbols.py``'s ``Symbol.file_id.in_([concrete ints])`` split.

Ranking (which candidate is "the" definition for a call site) runs in Python, AFTER query 2,
because every signal (``same_repo``/``same_file``/``kind_match``) is relational to the
``(site, candidate)`` pair -- computing it in SQL would require the self-join this module
avoids. Ranking is membership-preserving: a lower-ranked candidate is never dropped, only
sorted later, so genuine ambiguity (AC1) is never silently collapsed to one answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, Row, Select, Text, any_, func, literal, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import File, ReferenceEdge, Repo, Symbol
from app.query.compiler import DEFAULT_ROW_LIMIT
from app.search.errors import reraise_or_recoverable

# Per-request DB-time bound; a cancellation surfaces as QueryTooBroadError (mirrors symbols.py).
DEFAULT_STATEMENT_TIMEOUT_MS = 5000

# Per-name candidate ceiling: bounds the SQL FETCH (query 2's window), not just the payload.
DEFAULT_CANDIDATE_CAP = 32

# Call sites resolve to callables/constructors (`Foo()` is a constructor call). Import edges
# never earn this boost (D3/D4): `kind_match` is False for every import candidate uniformly.
CALL_TARGET_KINDS: frozenset[str] = frozenset({"function", "method", "class"})


# --------------------------------------------------------------------------- contract


@dataclass(frozen=True)
class CandidateSymbol:
    """One ranked candidate definition for an :class:`EdgeSite`.

    ``symbol_id`` is a query-time-transient internal tiebreak ONLY -- it is never persisted
    (nothing here writes a resolved id back to ``reference_edges``) and is excluded from the
    service-layer wire payload (see ``app.service._site_payload``), so carrying it does not
    violate the epic's "never add an extraction-time symbol FK" rule.
    """

    symbol_id: int
    repo_id: int
    path: str
    name: str
    kind: str | None
    start_line: int | None
    same_repo: bool
    same_file: bool
    kind_match: bool


@dataclass(frozen=True)
class EdgeSite:
    """One raw ``reference_edges`` row plus its resolved (ranked, possibly capped) candidates.

    ``candidate_count`` is the TRUE pre-cap count (SQL ``COUNT(*) OVER``, see
    :func:`_build_candidates_select`) -- it never shrinks just because the fetched/returned
    ``candidates`` list was capped, so ``resolution`` stays correct even under truncation.
    ``candidates_truncated`` is ``candidate_count > len(candidates)``.
    """

    repo_id: int
    file_id: int
    path: str
    line: int
    edge_kind: str  # "call" | "import"
    target_name: str
    enclosing_name: str | None
    enclosing_kind: str | None
    resolution: str  # "unique" | "ambiguous" | "unresolved"
    candidate_count: int
    candidates_truncated: bool
    candidates: tuple[CandidateSymbol, ...]


@dataclass(frozen=True)
class ReferenceResult:
    """Result of :func:`resolve_references`.

    ``repo_known`` is ``False`` iff a ``repo=`` scope was requested but no such repo exists --
    a structured miss (mirrors ``get_file_payload``'s ``found: False``), never a silent empty.
    It is always ``True`` when no repo scope was requested (nothing to be unknown about).
    """

    sites: tuple[EdgeSite, ...]
    truncated: bool  # site row-cap tripped
    truncation_reason: str | None  # "row_cap" | None
    repo_known: bool


# ----------------------------------------------------------------------- pure helpers


def classify_resolution(count: int) -> str:
    """Map a true candidate count to the ``resolution`` label. Shared with the measurement
    script (D10) so the offline distribution and the serve path cannot drift apart."""
    if count == 0:
        return "unresolved"
    if count == 1:
        return "unique"
    return "ambiguous"


def _branch_predicate(
    branch: str | None,
    *,
    file: type[File] = File,
    repo: type[Repo] = Repo,
) -> ColumnElement[bool]:
    """Branch-scoping predicate, byte-identical to ``get_file_payload``'s (``app/service.py``)
    and the query compiler's implicit default conjunct. ``file``/``repo`` default to the
    unaliased ORM classes (query 1 / query 2 each join ``files``/``repos`` exactly once); the
    measurement script's correlated subquery (D10) passes aliased entities for its inner
    ``symbols``-side join, which reaches a SECOND, distinct ``files``/``repos`` join in the
    same statement.
    """
    if branch is not None:
        return file.branches.op("@>")(literal([branch], type_=ARRAY(Text)))
    return func.coalesce(repo.default_branch, "HEAD") == any_(file.branches)


def _rank_candidates(candidates: list[CandidateSymbol]) -> tuple[CandidateSymbol, ...]:
    """Total-ordered, membership-preserving rank (D4): same-repo first, then kind-appropriate,
    then same-file, tiebreaking on ``(repo_id, path, start_line, symbol_id)`` for determinism.
    Never drops a candidate -- a lower-ranked one only sorts later.
    """
    return tuple(
        sorted(
            candidates,
            key=lambda c: (
                not c.same_repo,
                not c.kind_match,
                not c.same_file,
                c.repo_id,
                c.path,
                c.start_line or 0,
                c.symbol_id,
            ),
        )
    )


# ------------------------------------------------------------------------- SQL builders


def _build_sites_select(
    *,
    target_name: str | None,
    edge_kind: str | None,
    repo_id: int | None,
    branch: str | None,
    row_limit: int,
) -> Select:
    """Query 1: edge sites, touching ``reference_edges``/``files``/``repos`` only.

    Joins/orders through the authoritative ``File.repo_id`` (not the denormalized
    ``ReferenceEdge.repo_id``), mirroring ``symbols.py``'s same rule -- the branch predicate
    compares ``Repo.default_branch`` against ``File.branches``, so both must be the same repo.
    ``repo_id``, when given, filters ``ReferenceEdge.repo_id`` directly (index-served by
    ``ix_reference_edges_repo_kind``) rather than a post-join ``Repo.name`` predicate.
    """
    stmt = (
        select(
            ReferenceEdge.id,
            File.repo_id,
            ReferenceEdge.file_id,
            File.path,
            ReferenceEdge.line,
            ReferenceEdge.edge_kind,
            ReferenceEdge.target_name,
            ReferenceEdge.enclosing_name,
            ReferenceEdge.enclosing_kind,
        )
        .join(File, ReferenceEdge.file_id == File.id)
        .join(Repo, File.repo_id == Repo.id)
        .where(_branch_predicate(branch))
        .order_by(
            File.repo_id,
            File.path,
            ReferenceEdge.line,
            ReferenceEdge.id,
        )
        .limit(row_limit)
    )
    if target_name is not None:
        stmt = stmt.where(ReferenceEdge.target_name == target_name)
    if edge_kind is not None:
        stmt = stmt.where(ReferenceEdge.edge_kind == edge_kind)
    if repo_id is not None:
        stmt = stmt.where(ReferenceEdge.repo_id == repo_id)
    return stmt


def _build_candidates_select(*, names: list[str], branch: str | None, candidate_cap: int) -> Select:
    """Query 2: candidate symbols for ``names``, bounded IN SQL (not just in the payload).

    ``ROW_NUMBER() OVER (PARTITION BY symbols.name ORDER BY ...)`` keeps only the first
    ``candidate_cap`` rows per name by a name-intrinsic order (site-relative signals like
    ``same_repo``/``same_file`` can't be pushed here -- they depend on the site, which this
    query never sees). ``COUNT(*) OVER`` carries the TRUE pre-cap count out alongside the
    trimmed rows, so :func:`classify_resolution` stays exact even when the fetch is capped.
    """
    rn = (
        func.row_number()
        .over(
            partition_by=Symbol.name,
            order_by=(File.repo_id, File.path, Symbol.start_line, Symbol.id),
        )
        .label("rn")
    )
    total = func.count().over(partition_by=Symbol.name).label("candidate_count")
    inner = (
        select(
            Symbol.id.label("symbol_id"),
            Symbol.name,
            Symbol.kind,
            Symbol.start_line,
            File.repo_id,
            Symbol.file_id,
            File.path,
            rn,
            total,
        )
        .join(File, Symbol.file_id == File.id)
        .join(Repo, File.repo_id == Repo.id)
        .where(Symbol.name.in_(names), _branch_predicate(branch))
        .subquery()
    )
    return select(inner).where(inner.c.rn <= candidate_cap)


def build_candidate_count_select(*, edge_kind: str, branch: str | None) -> Select:
    """Per-site TRUE candidate count, reused by ``scripts/measure_reference_resolution.py``
    (D10) so the offline resolution-distribution measurement agrees with the serve path BY
    CONSTRUCTION rather than re-implementing the join. Uses a correlated scalar subquery
    (acceptable here: this builder has no per-request latency/timeout budget, unlike
    :func:`resolve_references`'s window-bounded query 2) over an ALIASED ``files``/``repos``
    join, since the outer ``reference_edges``/``files``/``repos`` join already occupies the
    unaliased names in this single statement.

    One row per matching edge: ``(edge_id, target_name, candidate_count)``.
    """
    sym_file = aliased(File)
    sym_repo = aliased(Repo)
    count_subq = (
        select(func.count())
        .select_from(Symbol)
        .join(sym_file, Symbol.file_id == sym_file.id)
        .join(sym_repo, sym_file.repo_id == sym_repo.id)
        .where(
            Symbol.name == ReferenceEdge.target_name,
            _branch_predicate(branch, file=sym_file, repo=sym_repo),
        )
        .correlate(ReferenceEdge)
        .scalar_subquery()
    )
    return (
        select(
            ReferenceEdge.id,
            ReferenceEdge.target_name,
            count_subq.label("candidate_count"),
        )
        .join(File, ReferenceEdge.file_id == File.id)
        .join(Repo, File.repo_id == Repo.id)
        .where(ReferenceEdge.edge_kind == edge_kind, _branch_predicate(branch))
    )


# --------------------------------------------------------------------- row -> dataclass


def _to_candidate(
    row: Row, *, site_repo_id: int, site_file_id: int, kind_eligible: bool
) -> CandidateSymbol:
    return CandidateSymbol(
        symbol_id=row.symbol_id,
        repo_id=row.repo_id,
        path=row.path,
        name=row.name,
        kind=row.kind,
        start_line=row.start_line,
        same_repo=row.repo_id == site_repo_id,
        same_file=row.file_id == site_file_id,
        kind_match=kind_eligible and row.kind in CALL_TARGET_KINDS,
    )


def _build_edge_site(site_row: Row, candidate_rows: list[Row]) -> EdgeSite:
    # kind_match eligibility is per-SITE (this edge's own kind), not the resolver's edge_kind
    # filter param -- an unfiltered corpus-wide resolve can mix call/import sites.
    kind_eligible = site_row.edge_kind == "call"
    candidate_count = candidate_rows[0].candidate_count if candidate_rows else 0
    candidates = _rank_candidates(
        [
            _to_candidate(
                row,
                site_repo_id=site_row.repo_id,
                site_file_id=site_row.file_id,
                kind_eligible=kind_eligible,
            )
            for row in candidate_rows
        ]
    )
    return EdgeSite(
        repo_id=site_row.repo_id,
        file_id=site_row.file_id,
        path=site_row.path,
        line=site_row.line,
        edge_kind=site_row.edge_kind,
        target_name=site_row.target_name,
        enclosing_name=site_row.enclosing_name,
        enclosing_kind=site_row.enclosing_kind,
        resolution=classify_resolution(candidate_count),
        candidate_count=candidate_count,
        candidates_truncated=candidate_count > len(candidates),
        candidates=candidates,
    )


# ------------------------------------------------------------------------ entry point


def resolve_references(
    conn: Connection,
    *,
    target_name: str | None = None,
    edge_kind: str | None = None,
    repo: str | None = None,
    branch: str | None = None,
    row_limit: int = DEFAULT_ROW_LIMIT,
    candidate_cap: int = DEFAULT_CANDIDATE_CAP,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> ReferenceResult:
    """Resolve raw ``reference_edges`` sites to ranked candidate-set ``symbols`` matches.

    ``target_name``/``edge_kind``/``repo`` are all optional filters (``find_references_payload``
    passes ``target_name``; ``list_imports_payload`` passes ``edge_kind="import"`` + a required
    ``repo``). ``branch`` is a PARAMETER (not a query atom), applied identically to both the
    edge site's file and each candidate's file (D6), mirroring ``get_file_payload``.

    Runs both queries in ONE transaction with a per-request ``statement_timeout``; a
    cancellation raises :class:`~app.search.errors.QueryTooBroadError` (uncaught here -- the
    service layer maps it, mirroring ``symbol_search``). A ``repo=`` scope that resolves to no
    repo short-circuits to an empty result with ``repo_known=False`` and NO further DB work.
    """
    with conn.begin():
        conn.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(statement_timeout_ms)},
        )

        repo_id: int | None = None
        if repo is not None:
            try:
                repo_id = conn.execute(
                    select(Repo.id).where(Repo.name == repo)
                ).scalar_one_or_none()
            except DBAPIError as error:
                reraise_or_recoverable(error)
            if repo_id is None:
                return ReferenceResult(
                    (), truncated=False, truncation_reason=None, repo_known=False
                )

        sites_stmt = _build_sites_select(
            target_name=target_name,
            edge_kind=edge_kind,
            repo_id=repo_id,
            branch=branch,
            row_limit=row_limit,
        )
        try:
            site_rows = conn.execute(sites_stmt).all()
        except DBAPIError as error:
            reraise_or_recoverable(error)

        truncated = len(site_rows) >= row_limit

        names = sorted({row.target_name for row in site_rows})
        candidates_by_name: dict[str, list[Row]] = {}
        if names:
            candidates_stmt = _build_candidates_select(
                names=names, branch=branch, candidate_cap=candidate_cap
            )
            try:
                candidate_rows = conn.execute(candidates_stmt).all()
            except DBAPIError as error:
                reraise_or_recoverable(error)
            for row in candidate_rows:
                candidates_by_name.setdefault(row.name, []).append(row)

    sites = tuple(
        _build_edge_site(row, candidates_by_name.get(row.target_name, [])) for row in site_rows
    )
    return ReferenceResult(
        sites=sites,
        truncated=truncated,
        truncation_reason="row_cap" if truncated else None,
        repo_known=True,
    )
