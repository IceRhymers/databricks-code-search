"""Atomic per-(repo, branch) upsert + content-SHA-keyed mark-and-sweep.

The caller supplies a live :class:`sqlalchemy.Connection` (mirroring the injected
connection seam in ``scripts/migrate.py``); ``index_repo`` owns the single atomic
unit of work via ``with conn.begin():`` for ONE branch. A mid-run failure rolls
the whole (repo, branch) transaction back, so the destructive sweep can never run
against a partially-written index. The caller's ``search_path`` is preserved --
this module never opens its own engine.

``files`` is content-deduped on ``(repo_id, path, content_sha)``, with membership
in a GIN-indexed ``branches`` array rather than one row per (repo, path). The
caller is expected to index a repo's branches SEQUENTIALLY within one worker --
that is what makes the sweep's plain ``UPDATE``/``DELETE`` safe without an
advisory lock: no other writer can touch this repo's rows concurrently. The
per-``(repo, branch)`` CAS baseline lives on ``repo_branches``, not ``repos``.
Only the default-branch run writes the deprecated ``repos`` legacy stamp, and it
does so WITHOUT a CAS check.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass

from sqlalchemy import Connection, delete, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import INDEX_SEMANTICS_VERSION, File, ReferenceEdge, Repo, RepoBranch, Symbol
from indexer.hashing import content_sha
from indexer.languages import FileExtraction, IndexCounts, ParsedFile

logger = logging.getLogger("indexer.store")


class StaleIndexError(RuntimeError):
    """The ``repo_branches`` row changed between this transaction's first and last statement.

    An invariant assertion, not an expected failure path: under the current
    single-run job model no second writer for one ``(repo, branch)`` can exist
    (branches within a repo are indexed sequentially by the same worker). It
    buys a loud failure the day that property is removed (``for_each_task``
    sharding, per-branch parallel fan-out, or a raised ``max_concurrent_runs``).
    It protects the *stamp* only -- it does not detect a refactor that moves the
    ``repo_branches`` upsert out of statement 2.
    """


@dataclass(frozen=True)
class ReconcileCounts:
    """Row-count summary returned by ``reconcile_retired_branches`` for one repo's run.

    ``files_stripped`` counts ``files`` rows whose ``branches`` array was
    modified (a distinct-files rowcount, not a pair count) -- it is deliberately
    not named ``memberships_stripped``, which would suggest one count per
    (file, branch) pair rather than per file. ``files_deleted`` is always a
    subset of ``files_stripped``: a row is only deleted once its subtraction
    leaves it with zero remaining branches.
    """

    branches_removed: int
    files_stripped: int
    files_deleted: int


# Called as chunk_writer(conn, repo_id, file_id, pf) once per file, inside the
# same conn.begin() as the rest of that file's row. Vectors must already be
# computed -- this seam never calls an embedder itself.
ChunkWriter = Callable[[Connection, int, int, ParsedFile], None]


def index_repo(
    conn: Connection,
    *,
    name: str,
    branch: str,
    is_default: bool,
    head_sha: str,
    items: Iterable[tuple[ParsedFile, FileExtraction]],
    chunk_writer: ChunkWriter | None = None,
) -> IndexCounts:
    """Upsert one ``(repo, branch)``'s files/symbols and sweep this branch's stale membership.

    All work runs inside a single ``with conn.begin():`` transaction:

    1. Upsert the ``repos`` row (keyed on ``name``) -> ``repo_id``. Only when
       ``is_default`` does this set ``default_branch`` and the deprecated legacy
       stamp columns (``last_indexed_commit`` / ``index_semantics_version`` /
       ``last_indexed_at``), written unconditionally (no CAS -- see module
       docstring). The ``ON CONFLICT DO UPDATE`` form is used even on a
       non-default run (a no-op ``SET name=name``) so ``RETURNING id`` always
       yields a row: ``DO NOTHING ... RETURNING`` returns nothing on conflict,
       which would break the ``repo_id`` bootstrap.
    2. Upsert/read the ``repo_branches`` row for ``(repo_id, branch)`` under its
       row lock, capturing ``(baseline_commit, baseline_version)`` -- the CAS
       baseline for step 5, mirroring the same ``RETURNING`` trick as step 1.
    3. Per file: an array-union upsert on ``uq_files_repo_path_sha`` -- a file
       whose content already exists under another branch gets THIS branch
       unioned into its ``branches`` array (one row, shared content); a file
       whose content differs from every existing version gets its own row. Then
       delete-and-reinsert its ``symbols`` and ``reference_edges`` (neither has a
       natural key), then call ``chunk_writer`` (if given) so chunk writes
       commit/roll back with the rest of that file's row. Each processed file's
       ``(path, content_sha)`` is collected into this branch's seen-set.
    4. Membership sweep, keyed on THIS branch's seen-set (never on ``commit``,
       which is ambiguous under dedup): strip ``branch`` from any row's
       ``branches`` array that is not in the seen-set, then delete any row left
       with an empty array (cascades ``symbols``/``chunks``/``reference_edges``). Pure DML, no
       ``TEMP TABLE`` (the job role has no guaranteed database-level TEMP
       privilege on Lakebase). **Skipped (with a WARNING) when the parsed file
       set is empty** -- an empty seen-set would otherwise strip ``branch`` from
       every row in the repo; conservatively skipping is safer than wiping.
    5. CAS-stamp the ``repo_branches`` row for ``(repo_id, branch)`` against the
       step-2 baseline (raises :class:`StaleIndexError` on mismatch).

    ``items`` may be a lazy generator; it is consumed inside the open
    transaction so memory stays bounded. ``chunk_writer`` defaults to ``None``,
    which makes this byte-identical to the core (semantic-off) path; when given,
    it must write PRECOMPUTED chunks -- embeddings are computed outside this
    transaction, so no network call ever happens here.
    """
    file_count = 0
    symbol_count = 0
    edge_count = 0
    seen_paths: list[str] = []
    seen_shas: list[str] = []

    with conn.begin():
        # MUST REMAIN STATEMENT 1 of this transaction -- see the docstring.
        repo_values: dict[str, object] = {"name": name}
        repo_set: dict[str, object] = {"name": name}
        if is_default:
            repo_values.update(
                default_branch=branch,
                last_indexed_commit=head_sha,
                index_semantics_version=INDEX_SEMANTICS_VERSION,
                last_indexed_at=func.now(),
            )
            repo_set = {k: v for k, v in repo_values.items() if k != "name"}
        repo_stmt = (
            pg_insert(Repo)
            .values(**repo_values)
            .on_conflict_do_update(index_elements=[Repo.name], set_=repo_set)
            .returning(Repo.id)
        )
        repo_id = conn.execute(repo_stmt).scalar_one()

        # Statement 2: the per-branch CAS baseline, on repo_branches now, not
        # repos. Same no-op-SET-on-conflict trick as statement 1.
        branch_stmt = (
            pg_insert(RepoBranch)
            .values(repo_id=repo_id, branch=branch)
            .on_conflict_do_update(
                constraint="uq_repo_branches",
                set_={"branch": branch},
            )
            .returning(RepoBranch.last_indexed_commit, RepoBranch.index_semantics_version)
        )
        baseline_commit, baseline_version = conn.execute(branch_stmt).one()

        for pf, ex in items:
            sha = content_sha(pf.content)
            file_stmt = (
                pg_insert(File)
                .values(
                    repo_id=repo_id,
                    path=pf.path,
                    lang=pf.lang,
                    size=pf.size,
                    content=pf.content,
                    commit=head_sha,
                    content_sha=sha,
                    branches=[branch],
                )
                .on_conflict_do_update(
                    constraint="uq_files_repo_path_sha",
                    set_={
                        "lang": pf.lang,
                        "size": pf.size,
                        "content": pf.content,
                        "commit": head_sha,
                        # Union this branch into whatever branches already share
                        # this exact content version -- a plain UNION via
                        # unnest+array_agg, row-lock-atomic regardless of
                        # concurrent readers (there is no concurrent WRITER for
                        # this repo -- see module docstring).
                        "branches": text(
                            "(SELECT array_agg(DISTINCT e) FROM "
                            "unnest(files.branches || excluded.branches) e)"
                        ),
                    },
                )
                .returning(File.id)
            )
            file_id = conn.execute(file_stmt).scalar_one()
            file_count += 1
            seen_paths.append(pf.path)
            seen_shas.append(sha)

            conn.execute(delete(Symbol).where(Symbol.file_id == file_id))
            if ex.symbols:
                conn.execute(
                    pg_insert(Symbol),
                    [
                        {
                            "file_id": file_id,
                            "repo_id": repo_id,
                            "name": s.name,
                            "kind": s.kind,
                            "start_line": s.start_line,
                            "end_line": s.end_line,
                        }
                        for s in ex.symbols
                    ],
                )
                symbol_count += len(ex.symbols)

            # UNCONDITIONAL, same as the symbols delete above: a file whose edges
            # all vanish (e.g. every call/import site removed) must shed its stale
            # rows even when this run's ex.edges is empty.
            conn.execute(delete(ReferenceEdge).where(ReferenceEdge.file_id == file_id))
            if ex.edges:
                conn.execute(
                    pg_insert(ReferenceEdge),
                    [
                        {
                            "file_id": file_id,
                            "repo_id": repo_id,
                            "edge_kind": e.kind,
                            "target_name": e.target,
                            "line": e.line,
                            "enclosing_name": e.enclosing.name if e.enclosing else None,
                            "enclosing_kind": e.enclosing.kind if e.enclosing else None,
                            "enclosing_start_line": e.enclosing.start_line if e.enclosing else None,
                            "enclosing_end_line": e.enclosing.end_line if e.enclosing else None,
                        }
                        for e in ex.edges
                    ],
                )
                edge_count += len(ex.edges)

            if chunk_writer is not None:
                chunk_writer(conn, repo_id, file_id, pf)

        swept = _sweep_membership(
            conn,
            name=name,
            branch=branch,
            repo_id=repo_id,
            seen_paths=seen_paths,
            seen_shas=seen_shas,
        )

        _stamp_repo_branch(
            conn,
            name=name,
            branch=branch,
            repo_id=repo_id,
            head_sha=head_sha,
            baseline_commit=baseline_commit,
            baseline_version=baseline_version,
        )

    return IndexCounts(files=file_count, symbols=symbol_count, swept=swept, edges=edge_count)


def _sweep_membership(
    conn: Connection,
    *,
    name: str,
    branch: str,
    repo_id: int,
    seen_paths: list[str],
    seen_shas: list[str],
) -> int:
    """Strip ``branch`` from any row not in this run's seen-set, then delete emptied rows.

    Pure DML via ``unnest`` of two parallel bound arrays -- no ``TEMP TABLE``
    (the job role has no guaranteed database-level TEMP privilege on Lakebase,
    see ``app/db/grants.py``). Expressed as raw SQL rather than SQLAlchemy Core:
    the anti-join against a two-column ``unnest(...)`` table-valued function has
    no materially clearer Core-expression form.

    **Empty seen-set guard**: if this branch parsed zero indexable files,
    skipping this sweep (WARN, return 0) is the conservative choice -- running
    it would strip ``branch`` from every row in the repo, wiping a branch's
    entire membership on a transient empty parse. A genuinely emptied branch
    simply retains stale membership until it next indexes non-empty (stale, but
    not wrong).
    """
    if not seen_paths:
        logger.warning(
            "%s@%s: parsed 0 indexable files; skipping the membership sweep "
            "(an empty seen-set would strip this branch from every file in the repo)",
            name,
            branch,
        )
        return 0

    removed = conn.execute(
        text(
            "UPDATE files SET branches = array_remove(branches, :branch) "
            "WHERE repo_id = :repo_id AND :branch = ANY(branches) "
            "AND NOT EXISTS (SELECT 1 FROM unnest(CAST(:paths AS text[]), CAST(:shas AS text[])) "
            "AS t(p, s) WHERE t.p = files.path AND t.s = files.content_sha)"
        ),
        {"branch": branch, "repo_id": repo_id, "paths": seen_paths, "shas": seen_shas},
    ).rowcount
    # The DELETE below only ever catches rows the UPDATE just emptied (no row
    # can already be at cardinality 0 entering a sweep -- every prior sweep
    # cleans those up too), so its rowcount is a SUBSET of ``removed``, not an
    # additional distinct file. ``swept`` counts distinct files this branch's
    # sweep affected -- one file whose only membership was this branch is one
    # swept file, whether it survives with an emptied-then-deleted row or (with
    # another branch still present) merely loses this branch from its array.
    conn.execute(
        text("DELETE FROM files WHERE repo_id = :repo_id AND cardinality(branches) = 0"),
        {"repo_id": repo_id},
    )
    return removed


def _stamp_repo_branch(
    conn: Connection,
    *,
    name: str,
    branch: str,
    repo_id: int,
    head_sha: str,
    baseline_commit: str | None,
    baseline_version: int | None,
) -> None:
    """Compare-and-set the ``repo_branches`` stamp against the statement-2 baseline.

    Raises :class:`StaleIndexError` if the row no longer matches the baseline,
    which propagates out of ``index_repo``'s ``conn.begin()`` and rolls the whole
    ``(repo, branch)`` transaction back rather than regressing the index.
    """
    result = conn.execute(
        update(RepoBranch)
        .where(
            RepoBranch.repo_id == repo_id,
            RepoBranch.branch == branch,
            RepoBranch.last_indexed_commit.is_not_distinct_from(baseline_commit),
            RepoBranch.index_semantics_version.is_not_distinct_from(baseline_version),
        )
        .values(
            last_indexed_commit=head_sha,
            index_semantics_version=INDEX_SEMANTICS_VERSION,
            last_indexed_at=func.now(),
        )
    )
    if result.rowcount != 1:
        raise StaleIndexError(
            f"{name}@{branch}: repo_branches row changed since this transaction's first "
            f"statement (baseline {baseline_commit!r}/{baseline_version!r}); aborting rather "
            "than regressing the index"
        )


def reconcile_retired_branches(
    conn: Connection,
    *,
    name: str,
    retired_branches: Collection[str],
) -> ReconcileCounts:
    """Remove retired branch membership from one repo's ``files`` and ``repo_branches``.

    A pure storage primitive for branches that are no longer actively indexed
    (deleted, default-branch changed, or narrowed out of a ``branches:`` glob)
    -- the case ``_sweep_membership`` cannot reach, since that sweep only runs
    during an active ``index_repo`` call for the branch being re-indexed. This
    helper does not decide which branches are retired; the caller supplies a set
    already proven retired, and it does not protect the live default branch from
    being passed in by mistake.

    **Sanitize first**: ``retired_branches`` is filtered to non-empty strings
    and de-duplicated before anything else. A ``None`` or blank entry bound
    into ``<> ALL(...)`` / ``= ANY(...)`` poisons the comparison (SQL's
    three-valued logic makes any ``= NULL`` comparison unknown, never true),
    which would make the membership-stripping ``WHERE e <> ALL(...)`` never
    evaluate true for that element and the rebuilt array come back with the
    row's *entire* membership intact instead of just the retired branches
    removed. Sanitizing before any SQL runs means an empty or all-invalid
    input returns ``(0, 0, 0)`` with no transaction opened at all -- it can
    never fall through into a wildcard match.

    Runs as a single ``with conn.begin():`` transaction, repo-scoped on every
    statement:

    1. ``SELECT id FROM repos WHERE name = :name FOR UPDATE`` resolves and
       locks the repo row in one statement; a missing repo is a no-op
       (``scalar_one_or_none`` returns ``None``). This lock makes the helper a
       per-repo mutex with ``index_repo`` (both take the repo row lock first),
       so the two can never interleave against the same repo. The job role
       holds ``UPDATE`` on this table (``app/db/grants.py``), so ``FOR UPDATE``
       is a privilege it already has.
    2. Strip every retired branch from ``files.branches`` for this repo via
       ``ARRAY(SELECT e FROM unnest(branches) AS e WHERE e <> ALL(...))``
       rather than this module's usual ``array_remove`` idiom (a deliberate
       deviation): it lets one GIN-served (``ix_files_branches_gin``) pass via
       ``branches && ...`` produce an exact distinct-files rowcount, and
       ``ARRAY(subquery)`` always yields ``'{}'`` rather than ``NULL`` -- a
       plain ``array_agg`` over an all-matched unnest returns ``NULL`` on an
       emptied array, which would leave a zombie row the next step's
       cardinality check can't see.
    3. Delete rows left with zero membership (``cardinality(branches) = 0``);
       a strict subset of step 2's rowcount. ``symbols``, ``chunks``, and
       ``reference_edges`` are removed by FK cascade, the same invariant
       ``_sweep_membership`` relies on (see its docstring, indexer/store.py).
    4. Delete the matching ``repo_branches`` registry rows.

    Invariants: repo-scoped on every statement; membership subtraction only,
    never delete-by-path (a shared ``(repo_id, path, content_sha)`` row keeps
    every branch it isn't losing); ``files.commit`` is never read (it is
    ambiguous under multi-branch dedup, see the module docstring); no engine
    construction, network I/O, or ``TEMP TABLE``; no Alembic migration; no
    ``INDEX_SEMANTICS_VERSION`` bump (this is not an indexing run); idempotent
    (re-running against an already-reconciled repo returns zero counts); and
    stored ``branches`` arrays are assumed NULL-element-free, guaranteed by
    ``index_repo``'s own writes.
    """
    retired = list(dict.fromkeys(b for b in retired_branches if isinstance(b, str) and b))
    if not retired:
        return ReconcileCounts(branches_removed=0, files_stripped=0, files_deleted=0)

    with conn.begin():
        repo_id = conn.execute(
            text("SELECT id FROM repos WHERE name = :name FOR UPDATE"),
            {"name": name},
        ).scalar_one_or_none()
        if repo_id is None:
            return ReconcileCounts(branches_removed=0, files_stripped=0, files_deleted=0)

        files_stripped = conn.execute(
            text(
                "UPDATE files SET branches = ARRAY("
                "SELECT e FROM unnest(branches) AS e WHERE e <> ALL(CAST(:retired AS text[]))"
                ") "
                "WHERE repo_id = :repo_id AND branches && CAST(:retired AS text[])"
            ),
            {"repo_id": repo_id, "retired": retired},
        ).rowcount

        files_deleted = conn.execute(
            text("DELETE FROM files WHERE repo_id = :repo_id AND cardinality(branches) = 0"),
            {"repo_id": repo_id},
        ).rowcount

        branches_removed = conn.execute(
            text(
                "DELETE FROM repo_branches "
                "WHERE repo_id = :repo_id AND branch = ANY(CAST(:retired AS text[]))"
            ),
            {"repo_id": repo_id, "retired": retired},
        ).rowcount

    return ReconcileCounts(
        branches_removed=branches_removed,
        files_stripped=files_stripped,
        files_deleted=files_deleted,
    )


def reconcile_removed_repos(conn: Connection, *, desired_repos: Collection[str]) -> list[str]:
    """Purge every ``repos`` row whose name is absent from ``desired_repos``.

    The counterpart to ``reconcile_retired_branches`` at the repo level -- a
    repo dropped entirely from the resolved corpus config (renamed, deleted
    upstream, or narrowed out of the config) is never revisited by any
    per-branch ``index_repo`` call, so nothing else in this module ever removes
    its row. This helper does not decide which repos are desired; the caller
    supplies the full resolved set, and it does not infer membership from
    anything already stored.

    **Guard is a deliberate INVERSION of ``reconcile_retired_branches``'s
    sanitizer.** There, ``retired_branches`` is the branches to *remove*, so
    filtering out poison entries is conservative (fewer removals). Here,
    ``desired_repos`` is the *keep* set: silently dropping an entry would
    *increase* what gets deleted. So this guard rejects instead of filtering --
    an empty collection, or any element that is not a non-empty ``str``, raises
    ``ValueError`` before any connection attribute is touched. This also closes
    the delete-everything hole: ``name <> ALL(CAST('{}' AS text[]))`` is
    vacuously true for every row, so an empty array reaching the DML below
    would purge the entire corpus. An empty ``desired_repos`` is always a
    caller bug (``resolve_repos`` already raises ``EmptyConfigError`` on an
    empty config), never a legitimate "delete everything" request.

    Runs as a single ``with conn.begin():`` transaction:

    1. ``DELETE FROM repos WHERE name <> ALL(CAST(:desired AS text[]))
       RETURNING name`` -- one atomic statement, no prior ``SELECT``. Every
       victim row's cascade is proven at the database level, not the ORM:
       ``repos`` -> ``files`` and ``repos`` -> ``symbols`` and ``repos`` ->
       ``repo_branches`` and ``repos`` -> ``reference_edges`` are direct
       ``ON DELETE CASCADE`` foreign keys (``app/db/models.py``), ``files`` ->
       ``symbols`` and ``files`` -> ``reference_edges`` are the same, and
       ``files`` -> ``chunks`` cascades via the raw DDL in
       ``app/alembic/versions/0004_semantic_chunks.py`` -- so a two-hop
       ``repos`` -> ``files`` -> ``chunks``/``reference_edges`` delete fires as
       one statement.
       ``RETURNING name`` reads back only ``repos`` rows, i.e. exactly the
       purged repo names, with no separate count query needed. The job role
       already holds ``DELETE`` on every table in this schema
       (``app/db/grants.py``), so no new grant is required.
    2. Matching is exact and case-sensitive, consistent with ``index_repo``'s
       upsert key and ``repos.name``'s unique constraint: a config respelling
       (``Acme/Widgets`` -> ``acme/widgets``) indexes a new row on the next
       clean run and this helper correctly purges the old-spelling row as a
       distinct name, rather than treating the two as the same repo.

    Invariants: never mutates or reads any row for a name in ``desired_repos``
    or any of their branches; never infers desired membership from what is
    already stored (the caller owns that decision); idempotent (re-running
    with the same ``desired_repos`` after a purge returns ``[]``); no engine
    construction, network I/O, or ``TEMP TABLE``; no Alembic migration; no
    ``INDEX_SEMANTICS_VERSION`` bump (this is not an indexing run); no
    ``FOR UPDATE``/advisory lock -- the victim rows are disjoint from whatever
    ``index_repo``/``reconcile_retired_branches`` may be locking concurrently,
    and the ``max_concurrent_runs: 1`` job pin already serializes indexer runs
    so this and a live indexing run never overlap in practice.

    A ``NULL`` element could never reach this DML: unlike
    ``reconcile_retired_branches``'s ``<> ALL`` poison risk (where an
    unsanitized ``NULL`` silently strands a row's membership untouched), this
    guard rejects any non-``str``/blank element outright before the query
    runs, so the under-deletion failure mode SQL's three-valued logic would
    otherwise produce here (a stray ``NULL`` making ``<> ALL`` evaluate
    ``UNKNOWN`` and the row survive) can never occur -- the caller gets a loud
    ``ValueError`` instead of a silently incomplete purge.
    """
    if not desired_repos:
        raise ValueError("desired_repos must not be empty (refusing to purge the entire corpus)")
    desired: list[str] = []
    for entry in desired_repos:
        if not isinstance(entry, str) or not entry:
            raise ValueError(f"desired_repos must contain only non-empty strings, got {entry!r}")
        desired.append(entry)
    desired = sorted(set(desired))

    with conn.begin():
        deleted = (
            conn.execute(
                text(
                    "DELETE FROM repos WHERE name <> ALL(CAST(:desired AS text[])) RETURNING name"
                ),
                {"desired": desired},
            )
            .scalars()
            .all()
        )

    return sorted(deleted)
