"""Atomic per-repo upsert + SHA-keyed mark-and-sweep into repos/files/symbols.

The caller supplies a live :class:`sqlalchemy.Connection` (mirroring the injected
connection seam in ``scripts/migrate.py``); ``index_repo`` owns the single atomic
unit of work via ``with conn.begin():``. A mid-run failure rolls the whole repo
back, so the destructive sweep can never run against a partially-written index
(plan Option A). The caller's ``search_path`` is preserved — this module never
opens its own engine.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, delete, func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.models import File, Repo, Symbol
from indexer.languages import ExtractedSymbol, IndexCounts, ParsedFile


def index_repo(
    conn: Connection,
    *,
    name: str,
    default_branch: str | None,
    head_sha: str,
    items: Iterable[tuple[ParsedFile, list[ExtractedSymbol]]],
) -> IndexCounts:
    """Upsert one repo's files/symbols and sweep rows not stamped with ``head_sha``.

    All work runs inside a single ``with conn.begin():`` transaction:

    1. Upsert the ``repos`` row (keyed on ``name``) -> ``repo_id``.
    2. Per file: upsert the ``files`` row (stamp ``commit=head_sha``), then
       delete-and-reinsert its ``symbols`` (symbols have no natural key).
    3. ``DELETE FROM files WHERE repo_id=:r AND commit<>:head_sha`` (cascade drops
       orphan symbols) -> ``swept``.
    4. Stamp ``repos.last_indexed_commit`` / ``last_indexed_at``.

    ``items`` may be a lazy generator; it is consumed inside the open transaction
    so memory stays bounded.
    """
    file_count = 0
    symbol_count = 0

    with conn.begin():
        repo_stmt = (
            pg_insert(Repo)
            .values(name=name, default_branch=default_branch)
            .on_conflict_do_update(
                index_elements=[Repo.name],
                set_={"default_branch": default_branch},
            )
            .returning(Repo.id)
        )
        repo_id = conn.execute(repo_stmt).scalar_one()

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

        sweep = conn.execute(delete(File).where(File.repo_id == repo_id, File.commit != head_sha))
        swept = sweep.rowcount

        conn.execute(
            update(Repo)
            .where(Repo.id == repo_id)
            .values(last_indexed_commit=head_sha, last_indexed_at=func.now())
        )

    return IndexCounts(files=file_count, symbols=symbol_count, swept=swept)
