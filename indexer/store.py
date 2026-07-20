"""Atomic per-repo upsert + SHA-keyed mark-and-sweep into repos/files/symbols.

The caller supplies a live :class:`sqlalchemy.Connection` (mirroring the injected
connection seam in ``scripts/migrate.py``); ``index_repo`` owns the single atomic
unit of work via ``with conn.begin():``. A mid-run failure rolls the whole repo
back, so the destructive sweep can never run against a partially-written index
(plan Option A). The caller's ``search_path`` is preserved — this module never
opens its own engine.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sqlalchemy import Connection, delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import INDEX_SEMANTICS_VERSION, File, Repo, Symbol
from indexer.languages import ExtractedSymbol, IndexCounts, ParsedFile


class StaleIndexError(RuntimeError):
    """The ``repos`` row changed between this transaction's first and last statement.

    An invariant assertion, not an expected failure path: under the current
    single-run job model no second writer for one repo can exist. It buys a loud
    failure the day that property is removed (``for_each_task`` sharding, or a
    raised ``max_concurrent_runs``). It protects the *stamp* only -- it does not
    detect a refactor that moves the ``repos`` upsert out of statement 1.
    """


# Called as chunk_writer(conn, repo_id, file_id, pf) once per file, inside the
# same conn.begin() as the rest of that file's row (issue #14 Phase 2). Vectors
# must already be computed -- this seam never calls an embedder itself (A4).
ChunkWriter = Callable[[Connection, int, int, ParsedFile], None]


def index_repo(
    conn: Connection,
    *,
    name: str,
    default_branch: str | None,
    head_sha: str,
    items: Iterable[tuple[ParsedFile, list[ExtractedSymbol]]],
    chunk_writer: ChunkWriter | None = None,
) -> IndexCounts:
    """Upsert one repo's files/symbols and sweep rows not stamped with ``head_sha``.

    All work runs inside a single ``with conn.begin():`` transaction:

    1. Upsert the ``repos`` row (keyed on ``name``) -> ``repo_id``.
    2. Per file: upsert the ``files`` row (stamp ``commit=head_sha``), then
       delete-and-reinsert its ``symbols`` (symbols have no natural key), then
       call ``chunk_writer`` (if given) so chunk writes commit/roll back with
       the rest of that file's row.
    3. ``DELETE FROM files WHERE repo_id=:r AND commit<>:head_sha`` (cascade drops
       orphan symbols and, in production, orphan chunks) -> ``swept``.
    4. Stamp ``repos.last_indexed_commit`` / ``index_semantics_version`` /
       ``last_indexed_at``, conditional on the row still matching the baseline
       read by step 1 (raises :class:`StaleIndexError` otherwise).

    ``items`` may be a lazy generator; it is consumed inside the open transaction
    so memory stays bounded. ``chunk_writer`` defaults to ``None``, which makes
    this byte-identical to the core (semantic-off) path (issue #14 AC-1); when
    given, it must write PRECOMPUTED chunks -- embeddings are computed outside
    this transaction (issue #14 A4), so no network call ever happens here.
    """
    file_count = 0
    symbol_count = 0

    with conn.begin():
        # MUST REMAIN STATEMENT 1 of this transaction. It takes the ``repos`` row
        # lock that is held to commit, and because ``last_indexed_commit`` /
        # ``index_semantics_version`` are absent from ``set_``, its RETURNING
        # yields their pre-update values -- the compare-and-set baseline, read
        # under the lock with no extra round trip and no TOCTOU window. Moving
        # this statement later silently invalidates the baseline; nothing below
        # will catch that.
        repo_stmt = (
            pg_insert(Repo)
            .values(name=name, default_branch=default_branch)
            .on_conflict_do_update(
                index_elements=[Repo.name],
                set_={"default_branch": default_branch},
            )
            .returning(Repo.id, Repo.last_indexed_commit, Repo.index_semantics_version)
        )
        # A brand-new repo yields (id, None, None); IS NOT DISTINCT FROM matches.
        repo_id, baseline_commit, baseline_version = conn.execute(repo_stmt).one()

        for pf, syms in items:
            file_stmt = (
                pg_insert(File)
                .values(
                    repo_id=repo_id,
                    path=pf.path,
                    lang=pf.lang,
                    size=pf.size,
                    content=pf.content,
                    commit=head_sha,
                )
                .on_conflict_do_update(
                    constraint="uq_files_repo_id_path",
                    set_={
                        "lang": pf.lang,
                        "size": pf.size,
                        "content": pf.content,
                        "commit": head_sha,
                    },
                )
                .returning(File.id)
            )
            file_id = conn.execute(file_stmt).scalar_one()
            file_count += 1

            conn.execute(delete(Symbol).where(Symbol.file_id == file_id))
            if syms:
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
                        for s in syms
                    ],
                )
                symbol_count += len(syms)

            if chunk_writer is not None:
                chunk_writer(conn, repo_id, file_id, pf)

        sweep = conn.execute(delete(File).where(File.repo_id == repo_id, File.commit != head_sha))
        swept = sweep.rowcount

        _stamp_repo(
            conn,
            name=name,
            repo_id=repo_id,
            head_sha=head_sha,
            baseline_commit=baseline_commit,
            baseline_version=baseline_version,
        )

    return IndexCounts(files=file_count, symbols=symbol_count, swept=swept)


def _stamp_repo(
    conn: Connection,
    *,
    name: str,
    repo_id: int,
    head_sha: str,
    baseline_commit: str | None,
    baseline_version: int | None,
) -> None:
    """Compare-and-set the ``repos`` stamp against the statement-1 baseline.

    Raises :class:`StaleIndexError` if the row no longer matches the baseline,
    which propagates out of ``index_repo``'s ``conn.begin()`` and rolls the whole
    repo back rather than regressing the index.
    """
    result = conn.execute(
        update(Repo)
        .where(
            Repo.id == repo_id,
            Repo.last_indexed_commit.is_not_distinct_from(baseline_commit),
            Repo.index_semantics_version.is_not_distinct_from(baseline_version),
        )
        .values(
            last_indexed_commit=head_sha,
            index_semantics_version=INDEX_SEMANTICS_VERSION,
            last_indexed_at=func.now(),
        )
    )
    if result.rowcount != 1:
        raise StaleIndexError(
            f"{name}: repos row changed since this transaction's first statement "
            f"(baseline {baseline_commit!r}/{baseline_version!r}); aborting rather "
            "than regressing the index"
        )
