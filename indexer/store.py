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
from indexer.timing import now, record

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

# The two projection reads behind the file-level delta path. Returned as
# ``(carried, present)`` -- see read_repo_content_shas.
ContentShaSets = tuple[set[tuple[str, str]], set[tuple[str, str]]]


def read_repo_content_shas(conn: Connection, *, repo_id: int, branch: str) -> ContentShaSets:
    """Project one repo's stored ``(path, content_sha)`` pairs, twice.

    Returns ``(carried, present)``:

    * ``carried`` -- the pairs on rows whose ``branches`` array already contains
      ``branch``. A parsed file in this set is UNCHANGED for this branch.
    * ``present`` -- every pair stored for this repo, on any branch. A parsed
      file in ``present - carried`` already exists as a row this branch does not
      yet carry, i.e. the membership-only class.

    **The projection is ``path, content_sha`` and nothing else, deliberately.**
    Two expensive mistakes are available here and both must stay closed:

    * selecting ``content`` pulls the entire corpus into the worker and defeats
      the whole point of the delta path;
    * selecting ``branches`` forces a heap fetch per row on a table whose rows
      carry that content, so the second read stops being an Index Only Scan.

    **They are also two separate statements on purpose.** Do NOT collapse them
    into ``SELECT path, content_sha, branches @> ... AS carries FROM files WHERE
    repo_id = :id``: that drops the branch predicate entirely (so
    ``ix_files_branches_gin`` is never consulted) *and* projects ``branches``.
    The containment form ``branches @> ARRAY[:branch]`` is what the GIN index can
    serve; ``_sweep_membership``'s ``:branch = ANY(branches)`` cannot be.

    **Keyed on ``(path, content_sha)``, NEVER on path alone.**
    ``uq_files_repo_path_sha`` permits several rows for one path with different
    content (the divergent-branch case), so a path-keyed dict silently drops rows
    and which one survives depends on row order.
    """
    carried = {
        (row.path, row.content_sha)
        for row in conn.execute(
            text(
                "SELECT path, content_sha FROM files "
                "WHERE repo_id = :repo_id AND branches @> CAST(:branch_arr AS text[])"
            ),
            {"repo_id": repo_id, "branch_arr": [branch]},
        )
    }
    present = {
        (row.path, row.content_sha)
        for row in conn.execute(
            text("SELECT path, content_sha FROM files WHERE repo_id = :repo_id"),
            {"repo_id": repo_id},
        )
    }
    return carried, present


def read_indexed_shas(conn: Connection, *, name: str, branch: str) -> ContentShaSets:
    """Name-keyed wrapper around :func:`read_repo_content_shas` for ``indexer.job``.

    Resolves ``repos.name -> id`` itself and returns two empty sets for a repo
    that has never been indexed (which degrades to "everything is changed/new" --
    safe in the correct direction). This is the ADVISORY copy of the read: the
    authoritative one runs inside ``index_repo``'s transaction. Both go through
    the same helper so there is exactly one pair of queries and one keying rule.

    Called on its own short-lived connection that is closed BEFORE embedding
    starts -- never on a connection held across network I/O.
    """
    repo_id = conn.execute(
        text("SELECT id FROM repos WHERE name = :name"), {"name": name}
    ).scalar_one_or_none()
    if repo_id is None:
        return set(), set()
    return read_repo_content_shas(conn, repo_id=int(repo_id), branch=branch)


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
    3. Two projection reads (:func:`read_repo_content_shas`), issued ONLY when
       the delta gate is open -- see below -- then, per file, a three-way
       classification on ``(pf.path, content_sha(pf.content))``:

       * **unchanged** (the pair is already on a row carrying this branch): no
         statement at all.
       * **membership-only** (the pair is stored for this repo but on a row this
         branch does not carry, AND statement 4 proves every branch of this repo
         is at the current semantics version): no symbol/edge work; the whole
         class is unioned in by ONE batched ``UPDATE ... RETURNING`` after the
         loop, which also supplies the ``file_id`` for its ``chunk_writer`` call
         (see :func:`_union_membership`).
       * **changed/new** (everything else): an array-union upsert on
         ``uq_files_repo_path_sha`` -- a file whose content already exists under
         another branch gets THIS branch unioned into its ``branches`` array (one
         row, shared content); a file whose content differs from every existing
         version gets its own row. Then delete-and-reinsert its ``symbols`` and
         ``reference_edges`` (neither has a natural key), then call
         ``chunk_writer`` (if given) so chunk writes commit/roll back with the
         rest of that file's row.

       **Every** parsed file -- classified or written -- is collected into this
       branch's seen-set, so step 4 and its empty-seen-set guard are correct by
       construction and untouched by the delta path.
    4. Membership sweep, keyed on THIS branch's seen-set (never on ``commit``,
       which is ambiguous under dedup): strip ``branch`` from any row's
       ``branches`` array that is not in the seen-set, then delete any row left
       with an empty array (cascades ``symbols``/``chunks``/``reference_edges``). Pure DML, no
       ``TEMP TABLE`` (the job role has no guaranteed database-level TEMP
       privilege on Lakebase). **Skipped (with a WARNING) when the parsed file
       set is empty** -- an empty seen-set would otherwise strip ``branch`` from
       every row in the repo; conservatively skipping is safer than wiping.
    5. CAS-stamp the ``repo_branches`` row for ``(repo_id, branch)`` against the
       step-2 baseline (raises :class:`StaleIndexError` on mismatch). A run whose
       seen-set was EMPTY advances ``last_indexed_commit`` but leaves
       ``index_semantics_version`` at the step-2 baseline -- it wrote nothing, so
       it indexed nothing at the current semantics version. See
       :func:`_stamp_repo_branch`.

    ``items`` may be a lazy generator; it is consumed inside the open
    transaction so memory stays bounded. ``chunk_writer`` defaults to ``None``,
    which makes this byte-identical to the core (semantic-off) path; when given,
    it must write PRECOMPUTED chunks -- embeddings are computed outside this
    transaction, so no network call ever happens here.

    **The delta gate.** The classification above is taken only when
    ``baseline_version == INDEX_SEMANTICS_VERSION`` -- statement 2's ``RETURNING``
    value, already in hand, no extra query. A ``NULL`` or older version means
    every file takes the full write path and statements 3a/3b are never issued.

    Why the unchanged path is sound, inductively:

    * A row carrying this branch was necessarily written by this branch's last
      COMPLETED run, and that run ran at ``baseline_version ==
      INDEX_SEMANTICS_VERSION``. In it the row was either written full-path (so
      it is current), or skipped as unchanged (current, by induction).
    * ... or acquired membership-only, which
      :func:`_repo_is_wholly_at_current_version` only permits when every branch
      of the repo is at the current version (so every surviving row of the repo
      was last written at it).
    * Base case: the first run after ANY version transition has
      ``baseline_version != INDEX_SEMANTICS_VERSION``, so it is full-path for
      every parsed file. A zero-parse run cannot manufacture a spurious base
      case -- it does not advance the version stamp (see
      :func:`_stamp_repo_branch`).

    Two columns are deliberately NOT re-derived for a skipped file:

    * ``lang`` and ``size`` are pure functions of ``(path, content)`` via
      ``indexer/parse.py`` + ``indexer/languages.py``, both watched by
      ``tests/unit/test_semantics_version_tripwire.py`` -- so a change to either
      derivation is MEANT to force a version bump, which closes this gate. That
      tripwire is a local-developer guard rather than a CI one, so treat this as
      a strong convention backed by review, not a machine-enforced invariant.
    * ``files.commit`` goes staler. No production read path exists (every
      ``commit:`` filter resolves from ``repo_branches.last_indexed_commit``,
      and the column is documented write-only and ambiguous under dedup in
      ``app/db/models.py``). This makes it staler; it makes nothing wrong.

    The breakdown is reported on one INFO line per call, immediately before the
    sweep, in both the gate-open and gate-closed cases::

        acme/widgets@main: delta write set 412/30214 files (unchanged=29790 membership=12,
        semantics gate open)

    ``IndexCounts`` is unchanged: ``files`` still counts files SEEN this run, and
    ``symbols``/``edges`` still count rows actually inserted -- so they
    legitimately read ``0`` on an all-unchanged run. That is the correct signal.
    """
    file_count = 0
    symbol_count = 0
    edge_count = 0
    unchanged_count = 0
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

        # Statements 3a/3b/4, issued only behind the delta gate. Everything the
        # classification below needs is now in hand; nothing else is read.
        delta_on = baseline_version == INDEX_SEMANTICS_VERSION
        carried: set[tuple[str, str]] = set()
        present: set[tuple[str, str]] = set()
        membership_ok = False
        if delta_on:
            carried, present = read_repo_content_shas(conn, repo_id=repo_id, branch=branch)
            membership_ok = _repo_is_wholly_at_current_version(conn, repo_id=repo_id)

        # (pf, content_sha) for each membership-only file, held until the batched
        # UPDATE below can hand back their file ids. A bounded exception to the
        # "items stream through the transaction" rule: membership-only is the
        # rare class (a branch ACQUIRING content another branch already stored),
        # not the steady state, and chunk_writer's seam takes the ParsedFile.
        membership: list[tuple[ParsedFile, str]] = []

        for pf, ex in items:
            sha = content_sha(pf.content)
            # Seen-set membership is recorded for EVERY parsed file, whatever its
            # class -- that is what keeps the sweep (and its empty-seen-set
            # guard) correct without any delta awareness of its own.
            file_count += 1
            seen_paths.append(pf.path)
            seen_shas.append(sha)

            if delta_on and (pf.path, sha) in carried:
                # Unchanged: this exact content is already stored on a row this
                # branch already carries. No file upsert, no symbol/edge
                # delete-reinsert, no chunk_writer call.
                unchanged_count += 1
                continue

            if delta_on and membership_ok and (pf.path, sha) in present:
                # Membership-only: the row exists (written by another branch) but
                # does not carry this branch yet. Statement 4 has proven every
                # branch of this repo is at the current semantics version, so its
                # symbols/edges are current and only the array union is owed.
                membership.append((pf, sha))
                continue

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

        # ONE statement for the whole membership-only class, skipped entirely
        # when that class is empty (rather than issued as a no-op) so the
        # statement inventory stays stable and greppable.
        if membership:
            _union_membership(
                conn,
                repo_id=repo_id,
                branch=branch,
                membership=membership,
                chunk_writer=chunk_writer,
            )

        # One INFO line per index_repo call, immediately before the sweep, in
        # BOTH the gate-open and gate-closed cases -- one format string, no
        # conditional fields, so the line is always present and always greppable.
        # The reason tail is the only part that varies. IndexCounts is
        # deliberately NOT extended to carry this: it is a frozen dataclass
        # compared by value in existing assertions, and `files` keeps meaning
        # "files seen this run" (the seen-set size).
        logger.info(
            "%s@%s: delta write set %d/%d files (unchanged=%d membership=%d, %s)",
            name,
            branch,
            file_count - unchanged_count - len(membership),
            file_count,
            unchanged_count,
            len(membership),
            "semantics gate open"
            if delta_on
            else f"semantics gate closed: stored v{baseline_version} != v{INDEX_SEMANTICS_VERSION}",
        )

        # Timed into indexer.job's ambient per-branch PhaseTimer, if one is
        # installed -- a no-op otherwise, so a direct index_repo call (tests,
        # scripts) is unaffected. Deliberately NOT a return value: IndexCounts is
        # a frozen dataclass compared by value in existing assertions, and NOT a
        # new index_repo parameter: that signature is an injected seam whose
        # fakes would all have to grow one. No log record is emitted here; the
        # number surfaces on job.py's single `phase timing` line.
        sweep_started = now()
        swept = _sweep_membership(
            conn,
            name=name,
            branch=branch,
            repo_id=repo_id,
            seen_paths=seen_paths,
            seen_shas=seen_shas,
        )
        record("sweep", now() - sweep_started)

        _stamp_repo_branch(
            conn,
            name=name,
            branch=branch,
            repo_id=repo_id,
            head_sha=head_sha,
            baseline_commit=baseline_commit,
            baseline_version=baseline_version,
            seen_any=bool(seen_paths),
        )

    return IndexCounts(files=file_count, symbols=symbol_count, swept=swept, edges=edge_count)


def _repo_is_wholly_at_current_version(conn: Connection, *, repo_id: int) -> bool:
    """Statement 4: is EVERY ``repo_branches`` row for this repo at the current version?

    The provenance gate the membership-only class depends on, and the hole
    ``(path, content_sha)`` alone does not close. Counter-example it exists for:
    branch ``b`` is stamped at the current version (delta on); sibling branch
    ``a`` was written at an OLDER version and has not re-indexed since. ``b``'s
    HEAD moves and acquires a file whose exact ``(path, content)`` already exists
    as ``a``'s stale-version row. Taking the membership-only path would skip the
    symbol/edge rewrite, so ``b`` would serve old-extractor symbols under a
    current-version stamp -- silently, and exactly the failure
    ``INDEX_SEMANTICS_VERSION`` exists to prevent.

    Given this gate, every surviving ``files`` row of the repo was last written
    at the current version: every row carries at least one branch (both sweep
    sites delete rows at ``cardinality(branches) = 0``), and every branch string
    on a ``branches`` array has a ``repo_branches`` row (``index_repo`` writes
    statement 2 before any file row for that branch, and
    ``reconcile_retired_branches`` deletes both in one transaction). Both
    directions are load-bearing and both are pinned by tests.
    """
    return bool(
        conn.execute(
            text(
                "SELECT NOT EXISTS (SELECT 1 FROM repo_branches "
                "WHERE repo_id = :repo_id "
                "AND index_semantics_version IS DISTINCT FROM :version)"
            ),
            {"repo_id": repo_id, "version": INDEX_SEMANTICS_VERSION},
        ).scalar_one()
    )


def _union_membership(
    conn: Connection,
    *,
    repo_id: int,
    branch: str,
    membership: list[tuple[ParsedFile, str]],
    chunk_writer: ChunkWriter | None,
) -> None:
    """Union ``branch`` into every membership-only row in ONE statement, then write their chunks.

    ``array_agg(DISTINCT ...)`` rather than ``||`` alone so the stored array
    stays sorted-distinct, matching ``index_repo``'s per-file upsert idiom --
    existing assertions compare ``branches`` by value.

    ``RETURNING id, path, content_sha`` supplies each row's ``file_id`` without a
    second lookup, which is what makes the ``chunk_writer`` call below possible.
    **Membership-only DOES write chunks** even though it writes no symbols or
    edges: the acquired row may legitimately have zero chunk rows (the branch
    that first wrote it ran semantic-off, or its precompute failed), and skipping
    the write would make that gap permanent for the acquiring branch where the
    full path would have filled it. The vectors are already in hand -- ``job.py``
    embeds every file the advisory read did not call unchanged.
    """
    paths = [pf.path for pf, _sha in membership]
    shas = [sha for _pf, sha in membership]
    rows = conn.execute(
        text(
            "UPDATE files SET branches = (SELECT array_agg(DISTINCT e) FROM "
            "unnest(files.branches || CAST(:branch_arr AS text[])) e) "
            "WHERE repo_id = :repo_id "
            "AND EXISTS (SELECT 1 FROM unnest(CAST(:paths AS text[]), CAST(:shas AS text[])) "
            "AS t(p, s) WHERE t.p = files.path AND t.s = files.content_sha) "
            "RETURNING id, path, content_sha"
        ),
        {"repo_id": repo_id, "branch_arr": [branch], "paths": paths, "shas": shas},
    ).all()

    if chunk_writer is None:
        return
    file_ids = {(row.path, row.content_sha): row.id for row in rows}
    for pf, sha in membership:
        file_id = file_ids.get((pf.path, sha))
        if file_id is None:
            # Unreachable while the single-writer invariant holds: the row was in
            # statement 3b's projection moments ago, inside this transaction.
            logger.warning(
                "membership-only row for %s vanished before its union; skipping its chunk write",
                pf.path,
            )
            continue
        chunk_writer(conn, repo_id, file_id, pf)


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
    seen_any: bool = True,
) -> None:
    """Compare-and-set the ``repo_branches`` stamp against the statement-2 baseline.

    Raises :class:`StaleIndexError` if the row no longer matches the baseline,
    which propagates out of ``index_repo``'s ``conn.begin()`` and rolls the whole
    ``(repo, branch)`` transaction back rather than regressing the index.

    **``seen_any=False`` holds the semantics version at ``baseline_version``.**
    A run that parsed zero indexable files (the transient case
    ``_sweep_membership``'s empty-seen-set guard exists for) has written nothing,
    so it has not indexed anything at the CURRENT semantics version and must not
    claim to have. Without this the following is silent and terminal:

    1. ``INDEX_SEMANTICS_VERSION`` goes 4 -> 5; branch ``b`` is stored at
       ``(sha1, 4)``, so the skip seam forces a re-index.
    2. That re-index parses zero files. Nothing is written -- but the stamp
       advances to ``(sha2, 5)``.
    3. The next run sees ``baseline_version == 5 == current``, opens the
       file-level delta gate, and finds every row carrying ``b`` unchanged on
       ``(path, content_sha)`` -- so it skips them all.
    4. ``b`` serves v4-extracted rows under a v5 stamp, permanently.

    ``last_indexed_commit`` still advances to ``head_sha``: the commit IS what
    this run looked at. Leaving the version behind is what makes the branch
    mismatch (and therefore re-index) on its next run -- self-healing, in the
    safe direction. The statement shape, the CAS predicate, and
    :class:`StaleIndexError` are untouched.

    **Known, deliberate divergence:** ``index_repo``'s statement 1 writes the
    DEPRECATED ``repos.index_semantics_version`` unconditionally on
    ``is_default``, with no seen-set awareness. So a zero-parse default-branch
    run leaves ``repos`` at the current version while ``repo_branches`` sits at
    the old one. That is cosmetic -- no decision anywhere reads
    ``repos.index_semantics_version`` (the three legacy columns are documented
    deprecated in ``app/db/models.py``) -- and extending this fix to the legacy
    stamp is scope this change deliberately does not take.
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
            index_semantics_version=(INDEX_SEMANTICS_VERSION if seen_any else baseline_version),
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
