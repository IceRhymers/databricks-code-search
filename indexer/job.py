"""Serverless indexing job entry point (``code-search-index``).

The repo set comes from the central ``config.yaml``, read from the workspace at
``--config`` on every run and resolved to canonical ``org/repo`` names by
:func:`indexer.resolve.resolve_repos`. Resolution completes in full -- config
read, enumeration, filtering, dedup, ceiling -- before the first tarball is
fetched, so a bad config exits non-zero having indexed nothing and without ever
opening a database connection.

Orchestrates, per resolved repo: resolve HEAD -> download the tarball by that
immutable SHA -> extract -> parse text files -> extract symbols -> atomic upsert +
mark-and-sweep via :func:`indexer.store.index_repo`. Each repo is isolated; the
process exits non-zero if any repo fails.

Repos are worked on concurrently by a bounded ``ThreadPoolExecutor`` sized by
``config.index_concurrency`` (clamped by
:func:`indexer.repo_config.effective_workers`). A repo whose stored
``(last_indexed_commit, index_semantics_version)`` already equals its current
HEAD SHA and :data:`app.db.models.INDEX_SEMANTICS_VERSION` is skipped before its
tarball is fetched. Because each repo's stamp is written inside that repo's own
transaction, a run killed halfway resumes on the next run with exactly the
remainder -- no checkpoint file, no resume flag. To force a re-index, clear the
provenance: ``UPDATE repos SET index_semantics_version = NULL`` (see
``docs/runbooks/indexing-parallelism.md``, which also names which identity holds
``UPDATE`` on ``repos``).

The engine's pool is DERIVED from that worker count rather than a constant, and
each worker checks local disk headroom before downloading anything -- a
shortfall fails that repo alone rather than the run.

That check is a PRE-FLIGHT SANITY CHECK, not admission control: it reserves
nothing, so N workers can each observe enough free space and then collectively
exhaust the disk. It reliably catches the steady-state case (disk already low
when a repo starts) and converts it into a legible per-repo error; it does NOT
bound the transient case, where an aggregate ENOSPC still surfaces as the opaque
``tarfile`` failure. Sizing ``index_concurrency`` to the disk is the real
control -- see ``docs/runbooks/indexing-parallelism.md``.

When ``cfg.semantic_enabled`` (issue #14), each repo's files are also chunked and
embedded -- but OUTSIDE ``index_repo``'s transaction (A4): the embedder is called
here, up front, and only a ``chunk_writer`` closure over the precomputed vectors
is handed to ``index_repo``, which writes them (pure DML, no network) inside the
same per-file loop as symbols. Flag-off: no chunking, no embedder, no import of
``indexer.embed``'s lazy ``databricks-sdk`` dependency.

Logging is INFO only. The GitHub token is read via an injected client and is never
logged, and this module never lowers root/SDK/httpx log levels (see the redaction
test + source-level tripwire).
"""

from __future__ import annotations

import argparse
import base64
import logging
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db.client import create_db_engine
from app.db.models import INDEX_SEMANTICS_VERSION, Repo
from indexer.chunk_store import write_chunks
from indexer.embed import EmbeddingCountMismatchError, EmbedFn, get_embedder
from indexer.fetch import (
    REQUIRED_FREE_BYTES,
    assert_disk_headroom,
    download_tarball,
    extract_tarball,
    resolve_ref,
)
from indexer.languages import Chunk, IndexCounts, ParsedFile
from indexer.parse import iter_chunks, iter_source_files
from indexer.repo_config import RepoConfig, effective_workers, load_config, normalize_repo
from indexer.resolve import MAX_REPOS, resolve_repos
from indexer.store import ChunkWriter, StaleIndexError, index_repo
from indexer.symbols import extract_symbols

logger = logging.getLogger("indexer.job")

# Per-repo log context. Set by _index_one so that records emitted by
# indexer.fetch / indexer.store / indexer.embed -- which carry no repo name of
# their own -- stay attributable once several repos interleave under fan-out.
# Default "-" covers every record emitted outside a worker (resolution, the
# main-thread drain loop, third-party libraries).
_repo_ctx: ContextVar[str] = ContextVar("repo", default="-")


class RepoLogFilter(logging.Filter):
    """Stamp ``record.repo`` from the per-repo context var.

    Installed on the root handler by :func:`main` so the ``[%(repo)s]`` field in
    the format string resolves for *every* record, including ones from modules
    that know nothing about this context.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.repo = _repo_ctx.get()
        return True


def read_github_token(client: Any, scope: str, key: str) -> str:
    """Read + base64-decode the GitHub token from Databricks secrets.

    ``client`` is an injected ``WorkspaceClient`` (or a fake in tests). Nothing
    here is logged — the token never touches a log record.
    """
    secret = client.secrets.get_secret(scope, key)
    return base64.b64decode(secret.value).decode()


def run(
    *,
    config_path: str,
    scope: str,
    key: str,
    endpoint: str | None,
    database: str | None,
    workspace_client: Any | None = None,
    http_client: httpx.Client | None = None,
    engine: Any | None = None,
    index_fn: Callable[..., IndexCounts] = index_repo,
    cfg: Settings | None = None,
    embed_fn: EmbedFn | None = None,
    config_loader: Callable[[Any, str], RepoConfig] = load_config,
    max_repos: int = MAX_REPOS,
) -> int:
    """Index every configured repo and return a process exit code (0 = all ok).

    Boundaries are injectable for tests: ``workspace_client`` (secret + config
    read), ``http_client`` (GitHub HTTP), ``engine`` (DB), ``index_fn`` (the
    store), ``embed_fn`` (issue #14 semantic chunking), and ``config_loader``
    (so orchestration tests need no SDK fake). ``cfg`` defaults to the
    process-cached :func:`app.config.get_settings`.

    ``resolve_repos`` is deliberately **not** injectable: the existing
    ``httpx.MockTransport`` seam already lets tests drive enumeration outcomes
    through the real resolver, and a fake one would let the fail-fast contract
    pass without exercising the wiring it exists to prove.
    """
    run_started = time.monotonic()
    if cfg is None:
        cfg = get_settings()

    if workspace_client is None:
        from databricks.sdk import WorkspaceClient

        workspace_client = WorkspaceClient()

    try:
        config = config_loader(workspace_client, config_path)
    except Exception:
        # Broad by design, matching the resolution handler below.
        # read_workspace_config wraps every SDK failure in ConfigError, but
        # parse_config only converts UnicodeDecodeError, yaml.YAMLError, and
        # ValidationError -- anything else out of yaml.safe_load would otherwise
        # escape as a raw traceback, degrading diagnosability at exactly the
        # point that motivated loading the config through the SDK.
        # exc_info is load-bearing, not stylistic: the chained SDK exception
        # carries the 403/404 that distinguishes "never synced" from "no read
        # permission". The message stays load-phase-specific so this and the
        # resolution failure below remain distinguishable in the log.
        logger.error("could not load config from %s", config_path, exc_info=True)
        return 1

    token = read_github_token(workspace_client, scope, key)

    owns_http = http_client is None
    if http_client is None:
        http_client = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=60.0)

    # Resolve BEFORE opening a database connection: a bad config must cost
    # nothing but the enumeration calls it already made.
    try:
        entries = resolve_repos(config, http_client, max_repos=max_repos)
    except Exception:
        # Broad by design. normalize_repo raises a bare ValueError, and
        # enumeration can raise httpx errors, RateLimitError, EmptyConfigError,
        # or RepoCeilingError; a named tuple would let a ValueError escape and
        # break main()'s exit-code contract.
        logger.error("could not resolve repos from %s", config_path, exc_info=True)
        if owns_http:
            http_client.close()
        return 1

    workers = effective_workers(config, semantic_enabled=cfg.semantic_enabled)
    if workers != config.index_concurrency:
        logger.info(
            "semantic enabled: clamping index_concurrency %d -> %d (memory bound: embedding "
            "materialises a whole repo's chunks per worker)",
            config.index_concurrency,
            workers,
        )

    owns_engine = engine is None
    if engine is None:
        # The pool is DERIVED from the worker count, not a constant: each worker
        # holds exactly one connection (one engine.connect() per repo, and the
        # embed/chunk precompute happens before it), so pool_size == workers is
        # exactly enough and max_overflow=0 turns a connection leak into a loud
        # stall instead of silent pool growth. pool_timeout is SQLAlchemy's own
        # default, spelled out HERE because max_overflow=0 is what makes it
        # observable -- a reader seeing the overflow ban must not have to go look
        # up how long the resulting stall lasts. Passing pool_size explicitly is
        # also what makes this correct in local mode, where create_db_engine
        # forwards **pool_kwargs raw and never applies its Lakebase setdefault.
        engine = create_db_engine(
            endpoint=endpoint,
            database=database,
            pool_size=workers,
            max_overflow=0,
            pool_timeout=30,
        )

    # Lazy embedder: built (and databricks-sdk imported) only when semantic
    # search is enabled and no fake was injected (issue #14 A1).
    #
    # Degrade rather than abort: get_embedder raises when semantic_embedding_endpoint is
    # unset, and letting that propagate would kill the WHOLE indexing run -- including every
    # repo that has nothing to do with semantic search. Semantic is an additive layer, so a
    # semantic misconfiguration must not cost us the core index.
    if cfg.semantic_enabled and embed_fn is None:
        try:
            embed_fn = get_embedder(cfg)
        except Exception:
            logger.warning(
                "semantic enabled but the embedder could not be built; indexing the core "
                "corpus WITHOUT chunks (semantic results will be stale until fixed)",
                exc_info=True,
            )
            embed_fn = None

    # Log the disk denominator every run. Serverless does not document the local
    # disk size, so this line is how the peak-usage arithmetic in
    # docs/runbooks/indexing-parallelism.md gets a real number to check against
    # -- and how a shrunken disk becomes visible before it becomes an outage.
    tmp_root = tempfile.gettempdir()
    usage = shutil.disk_usage(tmp_root)
    logger.info(
        "local disk at %s: %.1f GB free of %.1f GB total; %d worker(s) x %.1f GB peak",
        tmp_root,
        usage.free / 1e9,
        usage.total / 1e9,
        workers,
        REQUIRED_FREE_BYTES / 1e9,
    )

    ok = skipped = conflicts = failures = 0
    try:
        # One batched read for the whole run, BEFORE fan-out. Inside the try so a
        # failure here still closes the http client and disposes the engine.
        stamps = _read_stamps(engine, entries)

        # The pool nests INSIDE this try: __exit__ calls shutdown(wait=True), so
        # every worker is joined before the finally below closes the http client
        # and disposes the engine. Never shutdown(wait=False)/cancel_futures.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="index") as pool:
            futures = {
                pool.submit(
                    _index_one,
                    entry,
                    http_client=http_client,
                    engine=engine,
                    index_fn=index_fn,
                    cfg=cfg,
                    embed_fn=embed_fn,
                    stamp=stamps.get(entry.casefold(), (None, None)),
                ): entry
                for entry in entries
            }
            # as_completed drains on the MAIN thread, so all four counters are
            # single-threaded increments -- no lock, and fut.result() re-raises
            # the worker's exception here, preserving per-repo isolation exactly.
            for fut in as_completed(futures):
                entry = futures[fut]
                try:
                    counts = fut.result()
                except StaleIndexError as exc:
                    # The repos row changed under this worker, so its whole
                    # transaction rolled back and THIS REPO IS NOT INDEXED.
                    #
                    # Under the current single-run model (resources/job.yml sets
                    # queue.enabled with no max_concurrent_runs, and resolve_repos
                    # dedups) no second worker for one repo can exist -- so the
                    # realistic trigger is an operator running the documented
                    # force-reindex (UPDATE repos SET index_semantics_version =
                    # NULL) while a run is in flight. That is benign because it
                    # SELF-HEALS: the NULL stamp makes the next run re-index this
                    # repo unconditionally. That self-healing, not "the work was
                    # redundant", is why this is excluded from the exit code.
                    #
                    # Logged WITHOUT a traceback: a rolled-back transaction is
                    # not a crash, and the stack adds nothing an operator needs.
                    conflicts += 1
                    logger.warning(
                        "index conflict for %s (rolled back, not indexed; "
                        "will re-index next run): %s",
                        entry,
                        exc,
                    )
                    continue
                except Exception:
                    failures += 1
                    logger.exception("failed to index %s", entry)
                    continue
                if counts is None:
                    skipped += 1
                else:
                    ok += 1
                    logger.info(
                        "indexed %s: files=%d symbols=%d swept=%d",
                        entry,
                        counts.files,
                        counts.symbols,
                        counts.swept,
                    )
    finally:
        if owns_http:
            http_client.close()
        if owns_engine:
            engine.dispose()

    logger.info(
        "indexing complete: %d ok, %d skipped, %d conflicts, %d failed (of %d) in %.1fs",
        ok,
        skipped,
        conflicts,
        failures,
        len(entries),
        time.monotonic() - run_started,
    )
    # Conflicts do NOT fail the run because they SELF-HEAL -- the stamp that
    # displaced them makes the next run re-index those repos unconditionally.
    # Note this trades a paging signal for one run of staleness on those repos;
    # the WARNING above is the record. It is NOT that the work was redundant.
    return 1 if failures else 0


def _read_stamps(engine: Any, entries: list[str]) -> dict[str, tuple[str | None, int | None]]:
    """Read ``(last_indexed_commit, index_semantics_version)`` for ``entries``.

    One query for the whole run. ``.where(Repo.name.in_(entries))`` is
    load-bearing, not decoration: without it the result set is bounded by the
    *table*, and ``repos`` accumulates a row for every repo ever configured
    (dropping a repo from ``config.yaml`` never reaps it). With the filter the
    read is bounded by ``MAX_REPOS``.

    Keyed on ``casefold()`` purely as belt-and-braces. Note it is INERT for the
    matches this query can actually return: ``Repo.name.in_(entries)`` is
    case-SENSITIVE, so any row that comes back already equals its entry exactly,
    and ``r.name.casefold() == entry.casefold()`` iff ``r.name == entry``. It
    costs nothing and would save us if that filter ever became case-insensitive.

    What genuinely matters here is that ``normalize_repo`` is NOT hoisted to the
    main thread to build these keys: it raises a bare ``ValueError``, and called
    outside every per-repo handler it would break run()'s isolation contract.
    It stays inside ``_index_one``. A key that fails to match simply yields no
    stamp, which degrades to "index it": safe in the correct direction.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                Repo.name,
                Repo.last_indexed_commit,
                Repo.index_semantics_version,
            ).where(Repo.name.in_(entries))
        ).all()
    return {r.name.casefold(): (r.last_indexed_commit, r.index_semantics_version) for r in rows}


def _precompute_chunk_writer(
    files: list[ParsedFile], embed_fn: EmbedFn, max_chunks_per_repo: int
) -> ChunkWriter:
    """Chunk + embed every file up front (issue #14 A4: no network inside conn.begin()).

    Returns a :data:`indexer.store.ChunkWriter` closure over the precomputed
    ``(chunk_index, content, embedding)`` triples, keyed by file path, so
    ``index_repo`` can write them per file without ever calling the embedder
    itself. Raises ``ValueError`` if the repo's total chunk count exceeds
    ``max_chunks_per_repo`` (the documented hard ceiling, not a streaming bound
    -- see ``app.config.semantic_max_chunks_per_repo``).
    """
    per_file: dict[str, list[Chunk]] = {pf.path: list(iter_chunks(pf)) for pf in files}
    total = sum(len(chunks) for chunks in per_file.values())
    if total > max_chunks_per_repo:
        raise ValueError(
            f"repo has {total} chunks, exceeding semantic_max_chunks_per_repo={max_chunks_per_repo}"
        )

    all_texts = [c.content for chunks in per_file.values() for c in chunks]
    all_vectors = embed_fn(all_texts) if all_texts else []
    # Re-checked HERE, not just inside databricks_embedder: embed_fn is a public injection
    # point on run(), and the positional re-slicing below is what actually depends on the
    # invariant. A short result would raise IndexError further down; a long one would
    # silently attach the wrong file's vectors. Guard where the assumption is used.
    if len(all_vectors) != len(all_texts):
        raise EmbeddingCountMismatchError(
            f"embedder returned {len(all_vectors)} vectors for {len(all_texts)} chunk texts"
        )

    by_path: dict[str, list[tuple[int, str, list[float]]]] = {}
    i = 0
    for path, chunks in per_file.items():
        if not chunks:
            continue
        by_path[path] = [
            (c.chunk_index, c.content, all_vectors[i + j]) for j, c in enumerate(chunks)
        ]
        i += len(chunks)

    def chunk_writer(conn: Any, repo_id: int, file_id: int, pf: ParsedFile) -> None:
        write_chunks(conn, file_id=file_id, chunks=by_path.get(pf.path, []))

    return chunk_writer


def _index_one(
    entry: str,
    *,
    http_client: httpx.Client,
    engine: Any,
    index_fn: Callable[..., IndexCounts],
    cfg: Settings,
    embed_fn: EmbedFn | None,
    stamp: tuple[str | None, int | None] = (None, None),
) -> IndexCounts | None:
    """Run the full fetch -> parse -> symbols -> store pipeline for one repo entry.

    Returns ``None`` when the repo is already indexed at HEAD under the current
    semantics version -- no tarball, no temp dir, no connection, no disk.

    ``stamp`` is this repo's ``(last_indexed_commit, index_semantics_version)``
    as of the pre-fan-out read. A ``None`` version means the provenance of the
    stored index is unknown, so the repo is always re-indexed.
    """
    started = time.monotonic()
    # Set FIRST, before normalize_repo, so a malformed entry's ValueError still
    # logs under the right name. The finally reset is mandatory: ThreadPoolExecutor
    # reuses worker threads and does NOT reset the context between tasks, so
    # without it a failure raised before the next set() logs under this repo.
    token = _repo_ctx.set(entry)
    try:
        return _index_one_inner(
            entry,
            started=started,
            http_client=http_client,
            engine=engine,
            index_fn=index_fn,
            cfg=cfg,
            embed_fn=embed_fn,
            stamp=stamp,
        )
    finally:
        _repo_ctx.reset(token)


def _index_one_inner(
    entry: str,
    *,
    started: float,
    http_client: httpx.Client,
    engine: Any,
    index_fn: Callable[..., IndexCounts],
    cfg: Settings,
    embed_fn: EmbedFn | None,
    stamp: tuple[str | None, int | None],
) -> IndexCounts | None:
    """The body of :func:`_index_one`, run with the repo log context already set."""
    name = normalize_repo(entry)
    org, repo = name.split("/", 1)
    default_branch, head_sha = resolve_ref(http_client, org, repo)

    # The skip seam: after the immutable HEAD SHA is known, before anything is
    # downloaded. Both halves must match -- a stored NULL version never does.
    if stamp == (head_sha, INDEX_SEMANTICS_VERSION):
        logger.info(
            "skipped %s: already indexed at %s (semantics v%d) in %.2fs",
            name,
            head_sha,
            INDEX_SEMANTICS_VERSION,
            time.monotonic() - started,
        )
        return None

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Checked INSIDE the temp dir (so it measures the filesystem actually
        # being written to) and BEFORE the first byte is downloaded. Raising
        # here lands in run()'s per-repo isolation handler, so an over-committed
        # disk costs this repo and not the run.
        assert_disk_headroom(tmp_path, repo=name)
        tar_path = download_tarball(http_client, org, repo, head_sha, tmp_path)
        root = extract_tarball(tar_path, tmp_path / "extracted")

        chunk_writer: ChunkWriter | None = None
        if cfg.semantic_enabled and embed_fn is not None:
            # Chunking/embedding needs the full file list up front -- unlike the
            # lazy items generator below, it cannot stream through index_repo's
            # open transaction (A4).
            files = list(iter_source_files(root))
            try:
                chunk_writer = _precompute_chunk_writer(
                    files, embed_fn, cfg.semantic_max_chunks_per_repo
                )
            except Exception:
                # The semantic layer is ADDITIVE: a chunk-ceiling breach, a downed embedder,
                # or a dim/count mismatch must not cost this repo its core index. Letting it
                # propagate would skip files/symbols AND the mark-and-sweep, silently leaving
                # the repo stale -- a worse outcome than stale chunks. Chunks catch up on the
                # next successful run; the failure is logged with a traceback, never swallowed
                # silently.
                logger.warning(
                    "semantic precompute failed for %s; indexing core corpus without chunks",
                    name,
                    exc_info=True,
                )
                chunk_writer = None
            items = ((pf, extract_symbols(pf)) for pf in files)
        else:
            # Lazy generator: files stream through the open transaction (bounded memory).
            items = ((pf, extract_symbols(pf)) for pf in iter_source_files(root))

        with engine.connect() as conn:
            counts = index_fn(
                conn,
                name=name,
                default_branch=default_branch,
                head_sha=head_sha,
                items=items,
                chunk_writer=chunk_writer,
            )

    # Measured here, where the clock already runs, rather than as an IndexCounts
    # field: IndexCounts is a frozen dataclass compared by value in existing
    # assertions, and a nonzero-by-construction timing field would break them.
    # This is the instrument the "throughput measured on the first production
    # run" promise depends on -- without it that promise is unfalsifiable.
    logger.info("finished %s in %.2fs", name, time.monotonic() - started)
    return counts


def _positive_int(raw: str) -> int:
    """argparse type for ``--max_repos``: an int >= 1.

    ``--max_repos=0`` is well-defined but makes every non-empty resolution raise
    ``RepoCeilingError``, so reject it at the boundary rather than at 2am.
    """
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s [%(repo)s]: %(message)s")
    # On the HANDLER, not a logger: a logger-attached filter would not apply to
    # records propagated from indexer.fetch/store/embed, which is precisely the
    # interleaving the repo field exists to disambiguate.
    for handler in logging.getLogger().handlers:
        handler.addFilter(RepoLogFilter())
    # Capture the serverless interpreter version in the run log (M2 live gate).
    logger.info("code-search-index starting on Python %s", sys.version)

    parser = argparse.ArgumentParser(description="Index GitHub repos into the code search DB.")
    parser.add_argument("--config", required=True, help="Workspace path to config.yaml.")
    # Underscore, NOT hyphen: python_wheel_task emits each named_parameters key
    # verbatim as `--<key>=<value>`, and resources/job.yml spells it max_repos.
    parser.add_argument(
        "--max_repos",
        type=_positive_int,
        default=MAX_REPOS,
        help="Safety ceiling on the resolved repo count.",
    )
    parser.add_argument("--scope", required=True, help="Databricks secret scope for the GH token.")
    parser.add_argument("--key", required=True, help="Secret key within the scope.")
    parser.add_argument("--endpoint", default=None, help="Lakebase endpoint identifier.")
    parser.add_argument("--database", default=None, help="Postgres database name.")
    args = parser.parse_args()

    exit_code = run(
        config_path=args.config,
        max_repos=args.max_repos,
        scope=args.scope,
        key=args.key,
        endpoint=args.endpoint,
        database=args.database,
    )
    # A serverless python_wheel_task entry point signals success by returning
    # normally; any raised SystemExit (even code 0) is reported as a workload
    # failure. So only exit non-zero to fail the task when a repo failed.
    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
