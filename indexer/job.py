"""Serverless indexing job entry point (``code-search-index``).

The repo set comes from the central ``config.yaml``, read from the workspace at
``--config`` on every run and resolved to canonical ``org/repo`` names by
:func:`indexer.resolve.resolve_repos`. Resolution completes in full -- config
read, enumeration, filtering, dedup, ceiling -- before the first tarball is
fetched, so a bad config exits non-zero having indexed nothing and without ever
opening a database connection.

Orchestrates, per resolved repo: resolve the default branch's HEAD -> resolve the
repo's concrete branch list (config globs, plan Option A1) -> for each branch,
SEQUENTIALLY: resolve its HEAD SHA -> download the tarball by that immutable SHA
-> extract -> parse text files -> extract symbols -> atomic upsert + mark-and-sweep
via :func:`indexer.store.index_repo`. Branches within one repo are sequential
(never concurrent) -- that is the invariant that keeps ``store.py``'s per-branch
sweep sound without an advisory lock. Each BRANCH is isolated: one branch's
failure or CAS conflict does not stop its repo's other branches from being
attempted, and the process exits non-zero if any branch fails.

Each repo's worker returns a :class:`RepoOutcome` (#61): its per-branch
``outcomes`` plus ``discovery_complete``, mirroring
:class:`indexer.branches.BranchResolution` -- ``False`` when the soft branch
cap truncated that repo's concrete branch list, so its resolved set must not be
trusted as evidence the repo has no OTHER branches to reconcile against. This
run's drain loop still only reads ``outcomes`` for its ok/skipped/conflict/failed
counters, unchanged; ``discovery_complete`` is surfaced for a future
reconciliation gate (#59) to collect across repos, not consumed here.

One logical corpus writer, at the RUN level: ``resources/job.yml`` pins
``max_concurrent_runs: 1`` (queueing retained), so at most one run of this job
is ever fetching, deriving, or applying corpus state at a time -- the
invariant global desired-state reconciliation (#56) depends on to avoid two
runs deriving and applying different desired inventories concurrently. This is
a SEPARATE invariant from the per-branch sequencing above: it bounds
concurrent JOB RUNS, not what happens inside one run, and it does NOT cover a
writer outside this job entirely (a second job, an ad hoc ``bundle run``, or a
future per-repo/per-branch task-sharding split within one run). Any of those
would need a shared database fencing/lease protocol before it could coexist
with reconciliation -- see ``docs/runbooks/indexing-parallelism.md`` §1.1 for
the full invariant and its coverage boundary.

Repos are worked on concurrently by a bounded ``ThreadPoolExecutor`` sized by
``config.index_concurrency`` (clamped by
:func:`indexer.repo_config.effective_workers`) -- the pool's unit of work is one
REPO (all its branches, sequentially), not one branch. A branch whose stored
``repo_branches`` ``(last_indexed_commit, index_semantics_version)`` already
equals its current HEAD SHA and :data:`app.db.models.INDEX_SEMANTICS_VERSION` is
skipped before its tarball is fetched. Because each branch's stamp is written
inside that branch's own transaction, a run killed halfway resumes on the next
run with exactly the remainder -- no checkpoint file, no resume flag. To force a
re-index, clear the provenance: ``UPDATE repo_branches SET
index_semantics_version = NULL`` (see ``docs/runbooks/indexing-parallelism.md``,
which also names which identity holds ``UPDATE`` on ``repo_branches``).

The engine's pool is DERIVED from that worker count rather than a constant, and
each branch's fetch checks local disk headroom before downloading anything -- a
shortfall fails that branch alone rather than the run or even the rest of that
repo's branches.

That check is a PRE-FLIGHT SANITY CHECK, not admission control: it reserves
nothing, so N workers can each observe enough free space and then collectively
exhaust the disk. It reliably catches the steady-state case (disk already low
when a branch starts) and converts it into a legible per-branch error; it does
NOT bound the transient case, where an aggregate ENOSPC still surfaces as the
opaque ``tarfile`` failure. Sizing ``index_concurrency`` to the disk is the real
control -- see ``docs/runbooks/indexing-parallelism.md``.

The semantic knobs in ``cfg`` are not read straight from the environment: the
serverless job has no reachable ``CODE_SEARCH_*`` env surface, so ``run()``
overlays config.yaml's ``semantic:`` block onto ``cfg`` right after the config
loads (config.yaml > env > default; see
:class:`indexer.repo_config.SemanticOverrides`). Everything below -- the
``effective_workers`` clamp, the embedder build, the per-repo chunk cap -- reads
the overlaid ``cfg``.

When ``cfg.semantic_enabled`` (issue #14), each repo's files are also chunked and
embedded -- but OUTSIDE ``index_repo``'s transaction (A4): the embedder is called
here, up front, and only a ``chunk_writer`` closure over the precomputed vectors
is handed to ``index_repo``, which writes them (pure DML, no network) inside the
same per-file loop as symbols. Flag-off: no chunking, no embedder, no import of
``app.embed``'s lazy ``databricks-sdk`` dependency.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db.client import create_db_engine
from app.db.models import INDEX_SEMANTICS_VERSION, Repo, RepoBranch
from app.embed import EmbeddingCountMismatchError, EmbedFn, get_embedder
from indexer.branches import resolve_branches
from indexer.chunk_store import write_chunks
from indexer.fetch import (
    REQUIRED_FREE_BYTES,
    assert_disk_headroom,
    download_tarball,
    extract_tarball,
    list_branches,
    resolve_branch_head,
    resolve_ref,
)
from indexer.languages import Chunk, IndexCounts, ParsedFile
from indexer.parse import iter_chunks, iter_source_files
from indexer.repo_config import RepoConfig, effective_workers, load_config, normalize_repo
from indexer.resolve import MAX_REPOS, RepoEntry, resolve_repos
from indexer.store import (
    ChunkWriter,
    ReconcileCounts,
    StaleIndexError,
    index_repo,
    reconcile_removed_repos,
    reconcile_retired_branches,
)
from indexer.symbols import extract_symbols

logger = logging.getLogger("indexer.job")

# Corpus-wide desired-state reconciliation (#56/#59) withholds the repo purge --
# but NOT retired-branch cleanup on surviving repos -- when it would remove more
# than this fraction of currently stored repos in one clean run. A narrowed
# GitHub token/org scope returns a clean HTTP 200 with fewer repos than before,
# which is indistinguishable from a legitimate mass decommission by count alone;
# this guard converts a silent mass purge into a loud, recoverable incident
# instead. Strict ``>``: a purge removing EXACTLY half proceeds. No config knob --
# see indexer/AGENTS.md for the rationale and the staged-removal remedy.
MAX_PURGE_SHRINK_FRACTION = 0.5


@dataclass(frozen=True)
class BranchOutcome:
    """The result of attempting one branch within a repo's sequential loop.

    ``counts`` is ``None`` for every status except ``"indexed"``. Classified the
    SAME way ``run()``'s drain loop used to classify a whole repo's future
    (``StaleIndexError`` -> ``"conflict"``, any other exception -> ``"failed"``)
    -- but now caught INSIDE the per-branch loop so one branch's failure never
    stops its repo's other branches from being attempted.
    """

    branch: str
    status: Literal["indexed", "skipped", "conflict", "failed"]
    counts: IndexCounts | None = None


@dataclass(frozen=True)
class RepoOutcome:
    """The result of attempting every branch of one repo (#61).

    ``discovery_complete`` mirrors :class:`indexer.branches.BranchResolution`'s
    ``complete`` flag for this repo's concrete branch list: ``False`` means the
    soft cap truncated discovery, so ``outcomes`` covers only the KEPT branches
    and must never be read as this repo's full branch membership. It is
    production-WRITE-only here -- ``run()``'s drain loop still unpacks only
    ``outcomes`` for its ok/skipped/conflict/failed counters, unchanged from
    before this field existed. Collecting ``discovery_complete`` across repos to
    gate reconciliation is #59's job, not this one's; this dataclass exists so
    that collection has something typed to read once it lands.

    A repo that fails BEFORE this object is constructed (a bad default-HEAD
    resolve, a branch-listing failure) has no ``RepoOutcome`` at all -- it
    propagates as an exception and is still counted in ``run()``'s ``except``
    branch, exactly as before. That absence is itself meaningful: it is a
    stronger signal than ``discovery_complete=False`` and must stay
    distinguishable from it, which is why this field is never defaulted to
    ``False`` for a repo that never got this far.
    """

    name: str
    discovery_complete: bool
    outcomes: list[BranchOutcome]


@dataclass
class ReconcileProgress:
    """Mutable accumulator for the post-fan-out reconciliation checkpoint (#59).

    Deliberately NOT frozen: ``_reconcile`` mutates one instance in place as it
    walks the retired-branch loop and then the purge decision, so a mid-sequence
    exception still leaves an accurate partial record for the failure-message
    split below.

    ``committed_any`` is the field that split hinges on: it is set ``True`` as
    the FIRST statement immediately after each of ``reconcile_retired_fn`` /
    ``reconcile_removed_fn`` RETURNS -- both primitives are proven in
    ``indexer/store.py`` to either fully commit and return or roll back and
    raise, never partially commit, so "the call returned" is exactly equivalent
    to "that primitive's transaction committed". A later exception (from a
    *different* call) with ``committed_any=True`` means the corpus is honestly
    PARTIALLY reconciled, not silently stale -- see ``_reconcile``.
    """

    committed_any: bool = False
    branches_removed: int = 0
    files_stripped: int = 0
    files_deleted: int = 0
    purged_repos: list[str] = field(default_factory=list)
    purge_blocked: bool = False
    would_purge_count: int = 0
    stored_count: int = 0


# Per-repo log context. Set by _index_one so that records emitted by
# indexer.fetch / indexer.store / app.embed -- which carry no repo name of
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
    reconcile_retired_fn: Callable[..., ReconcileCounts] = reconcile_retired_branches,
    reconcile_removed_fn: Callable[..., list[str]] = reconcile_removed_repos,
) -> int:
    """Index every configured repo and return a process exit code (0 = all ok).

    Boundaries are injectable for tests: ``workspace_client`` (secret + config
    read), ``http_client`` (GitHub HTTP), ``engine`` (DB), ``index_fn`` (the
    store), ``embed_fn`` (issue #14 semantic chunking), ``config_loader`` (so
    orchestration tests need no SDK fake), and ``reconcile_retired_fn`` /
    ``reconcile_removed_fn`` (the #57/#58 storage primitives the post-fan-out
    checkpoint below invokes -- mirroring ``index_fn``'s injection so
    reconciliation tests need no real Postgres either). ``cfg`` defaults to the
    process-cached :func:`app.config.get_settings`, then any ``semantic:`` fields
    in config.yaml are overlaid onto it before the worker clamp and embedder build
    (config.yaml > env > default -- config.yaml is the job's semantic-config
    surface; see :class:`indexer.repo_config.SemanticOverrides`).

    ``resolve_repos`` is deliberately **not** injectable: the existing
    ``httpx.MockTransport`` seam already lets tests drive enumeration outcomes
    through the real resolver, and a fake one would let the fail-fast contract
    pass without exercising the wiring it exists to prove.

    Reconciliation (#56/#59) is invoked ONLY from this function's post-fan-out
    checkpoint, main-thread, after every worker has joined -- never from
    ``_index_one``/``_index_one_inner``/``_index_one_branch``. See
    ``test_reconcile_fns_never_referenced_from_worker_source`` in
    ``tests/unit/test_job.py`` for the tripwire that enforces it.
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

    # config.yaml is the job's semantic-config surface (env has no reachable
    # surface for the serverless job -- resources/job.yml sets no CODE_SEARCH_*).
    # Overlay any `semantic:` fields the operator set onto cfg HERE, before the
    # worker clamp and the embedder build below, so effective_workers() and
    # get_embedder() both see config.yaml's values -- config.yaml > env > default.
    # An absent/empty block yields {} and leaves the injected cfg untouched (the
    # test seam keeps working: overlay applies to whatever cfg is in hand). Only
    # the SET field NAMES are logged, never values -- same redaction posture as
    # the rest of this module (endpoints/models are not secrets, but logging only
    # names keeps the "this module logs no config values" invariant simple).
    overrides = config.semantic.settings_overrides()
    if overrides:
        cfg = cfg.model_copy(update=overrides)
        logger.info("config.yaml semantic overrides applied: %s", sorted(overrides))

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
    ok = skipped = conflicts = failures = 0
    repo_outcomes: list[RepoOutcome] = []
    reconciliation_attempted = False
    reconciliation_failed = False
    reconcile_skip_reason = ""
    progress = ReconcileProgress()
    try:
        # Inside the try for the same reason as the stamp read below: shutil
        # .disk_usage raises if tmp is missing or unmounted, and above the try
        # that would skip the finally and leak the http client and the engine.
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
                    stamps=stamps,
                ): entry
                for entry in entries
            }
            # as_completed drains on the MAIN thread, so all four counters are
            # single-threaded increments -- no lock. Per-branch failures/conflicts
            # are now classified INSIDE _index_one_inner's loop (so one bad branch
            # never stops its repo's other branches) -- fut.result() only raises
            # for a failure BEFORE that loop starts (resolving the default HEAD,
            # listing branches): those remain repo-level and are still caught here.
            for fut in as_completed(futures):
                entry = futures[fut]
                try:
                    repo_outcome = fut.result()
                except Exception:
                    failures += 1
                    logger.exception("failed to index %s", entry.name)
                    continue
                repo_outcomes.append(repo_outcome)
                for outcome in repo_outcome.outcomes:
                    if outcome.status == "skipped":
                        skipped += 1
                    elif outcome.status == "conflict":
                        conflicts += 1
                    elif outcome.status == "failed":
                        failures += 1
                    else:
                        ok += 1
                        assert outcome.counts is not None
                        logger.info(
                            "indexed %s@%s: files=%d symbols=%d swept=%d",
                            entry.name,
                            outcome.branch,
                            outcome.counts.files,
                            outcome.counts.symbols,
                            outcome.counts.swept,
                        )

        # Desired-state reconciliation checkpoint (#56/#59). MUST stay HERE: after
        # the ThreadPoolExecutor `with` above has exited (every worker joined --
        # __exit__ calls shutdown(wait=True)) and before the `finally` below (the
        # engine is still open). `stamps` is the snapshot _read_stamps took BEFORE
        # fan-out, reused as-is -- NOT re-read now. That is only sound because of
        # the SAME single-writer invariant this module's docstring names:
        # resources/job.yml pins max_concurrent_runs: 1 and there is no in-run
        # sharding, so from the moment `stamps` was read to this line, the only
        # writer that could have touched `repo_branches` is THIS run's own workers
        # -- and they only ever ADD a row for a branch they just resolved and
        # indexed, never remove one. So "persisted minus this run's resolved set"
        # reads identically off the pre-fan-out snapshot as it would off a fresh
        # post-join read. Raising max_concurrent_runs, adding per-repo/per-branch
        # task sharding, or any writer outside this job (see
        # docs/runbooks/indexing-parallelism.md §1.1) would invalidate this reuse
        # -- do not relax it without re-deriving this proof.
        reconciliation_attempted = _decide_reconciliation(
            failures=failures,
            conflicts=conflicts,
            repo_outcomes=repo_outcomes,
            entries=entries,
        )
        if reconciliation_attempted:
            progress, reconciliation_failed = _reconcile(
                engine,
                repo_outcomes=repo_outcomes,
                stamps=stamps,
                reconcile_retired_fn=reconcile_retired_fn,
                reconcile_removed_fn=reconcile_removed_fn,
            )
        else:
            reconcile_skip_reason = _reconciliation_skip_reason(
                failures=failures,
                conflicts=conflicts,
                repo_outcomes=repo_outcomes,
                entries=entries,
            )
    finally:
        if owns_http:
            http_client.close()
        if owns_engine:
            engine.dispose()

    logger.info(
        "indexing complete: %d branch(es) ok, %d skipped, %d conflicts, %d failed "
        "(across %d repos) in %.1fs",
        ok,
        skipped,
        conflicts,
        failures,
        len(entries),
        time.monotonic() - run_started,
    )
    # Conflicts do NOT fail the run because they SELF-HEAL -- the stamp that
    # displaced them makes the next run re-index that branch unconditionally.
    # Note this trades a paging signal for one run of staleness on that branch;
    # the WARNING logged at the conflict site is the record. It is NOT that the
    # work was redundant.

    # Exactly one reconciliation summary line, always after "indexing complete".
    # The failure/withheld path already logged its own ERROR incident line
    # inside _reconcile -- this branch must not double-log it.
    if reconciliation_failed or progress.purge_blocked:
        pass
    elif reconciliation_attempted:
        logger.info(
            "corpus reconciliation complete: %d branch(es) retired (%d file(s) stripped, "
            "%d deleted), %d repo(s) purged",
            progress.branches_removed,
            progress.files_stripped,
            progress.files_deleted,
            len(progress.purged_repos),
        )
    else:
        logger.info("corpus reconciliation skipped: %s", reconcile_skip_reason)

    return 1 if (failures or reconciliation_failed or progress.purge_blocked) else 0


def _read_stamps(
    engine: Any, entries: list[RepoEntry]
) -> dict[tuple[str, str], tuple[str | None, int | None]]:
    """Read every existing ``repo_branches`` ``(last_indexed_commit, index_semantics_version)``
    for ``entries``, keyed by ``(name.casefold(), branch)``.

    One query for the whole run, BEFORE branch resolution. ``.where(Repo.name.in_(names))`` is
    load-bearing, not decoration: without it the result set is bounded by the
    *table*, and ``repo_branches`` accumulates rows for every repo/branch ever
    configured (dropping a repo -- or narrowing its globs -- from ``config.yaml``
    never reaps them). With the filter the read is bounded by ``MAX_REPOS`` times
    each repo's branch count.

    Branch resolution needs a GitHub API call (``indexer.fetch.list_branches``),
    which cannot happen before fan-out without serializing every repo's branch
    listing on the main thread -- so this reads ALL of a repo's existing
    ``repo_branches`` rows regardless of which branches this run will actually
    resolve, and each worker looks up ``stamps.get((name, branch))`` per branch
    as it discovers them. A branch that has never been indexed simply misses,
    which degrades to "index it": safe in the correct direction.

    Keyed on ``casefold()`` purely as belt-and-braces. Note it is INERT for the
    matches this query can actually return: ``Repo.name.in_(names)`` is
    case-SENSITIVE, so any row that comes back already equals its entry exactly,
    and ``r.name.casefold() == entry.name.casefold()`` iff ``r.name ==
    entry.name``. It costs nothing and would save us if that filter ever became
    case-insensitive.

    What genuinely matters here is that ``normalize_repo`` is NOT hoisted to the
    main thread to build these keys: it raises a bare ``ValueError``, and called
    outside every per-repo handler it would break run()'s isolation contract. It
    stays inside ``_index_one``.
    """
    names = [entry.name for entry in entries]
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                Repo.name,
                RepoBranch.branch,
                RepoBranch.last_indexed_commit,
                RepoBranch.index_semantics_version,
            )
            .select_from(RepoBranch)
            .join(Repo, Repo.id == RepoBranch.repo_id)
            .where(Repo.name.in_(names))
        ).all()
    return {
        (r.name.casefold(), r.branch): (r.last_indexed_commit, r.index_semantics_version)
        for r in rows
    }


def _decide_reconciliation(
    *,
    failures: int,
    conflicts: int,
    repo_outcomes: list[RepoOutcome],
    entries: list[RepoEntry],
) -> bool:
    """Pure gate: may this run's post-fan-out checkpoint reconcile desired state?

    Fail-closed by construction -- every conjunct must hold:

    - ``failures == 0``: no repo-level failure and no branch ``failed`` outcome
      (both already fold into this counter in ``run()``'s drain loop).
    - ``conflicts == 0``: a CAS conflict means that branch was NOT indexed this
      run, so its true current state is unknown; it self-heals next run (see
      ``run()``'s conflict comment) but must not authorize reconciliation now.
    - ``len(repo_outcomes) == len(entries)``: every resolved repo produced a
      :class:`RepoOutcome` (a pre-outcome exception already counts as a
      failure and is excluded above) -- redundant with ``failures == 0`` given
      ``run()``'s current control flow, kept anyway as defence-in-depth against
      a future refactor that decouples the two.
    - ``all(o.discovery_complete for o in repo_outcomes)``: a soft-branch-cap
      truncation (see :class:`indexer.branches.BranchResolution`) means that
      repo's resolved branch set is not its FULL membership, so branches
      outside the cap must never be treated as retired.

    ``skipped`` branch outcomes count as success -- they are not represented in
    any of these counters, so a fully-skipped run passes the gate.
    """
    return (
        failures == 0
        and conflicts == 0
        and len(repo_outcomes) == len(entries)
        and all(o.discovery_complete for o in repo_outcomes)
    )


def _reconciliation_skip_reason(
    *,
    failures: int,
    conflicts: int,
    repo_outcomes: list[RepoOutcome],
    entries: list[RepoEntry],
) -> str:
    """Aggregate, human-readable reason(s) :func:`_decide_reconciliation` returned ``False``.

    Ordered to mirror that function's conjuncts so the two can never silently
    drift apart. Falls back to ``"unknown"`` only as a defensive catch-all --
    every path that can make the gate fail is enumerated above it and should
    always contribute at least one reason.
    """
    reasons: list[str] = []
    if failures:
        reasons.append(f"{failures} repo/branch failure(s)")
    if conflicts:
        reasons.append(f"{conflicts} branch conflict(s)")
    missing = len(entries) - len(repo_outcomes)
    if missing:
        reasons.append(f"{missing} repo(s) never completed")
    incomplete = sum(1 for o in repo_outcomes if not o.discovery_complete)
    if incomplete:
        reasons.append(f"{incomplete} repo(s) with incomplete branch discovery")
    return "; ".join(reasons) if reasons else "unknown"


def _reconcile(
    engine: Any,
    *,
    repo_outcomes: list[RepoOutcome],
    stamps: dict[tuple[str, str], tuple[str | None, int | None]],
    reconcile_retired_fn: Callable[..., ReconcileCounts],
    reconcile_removed_fn: Callable[..., list[str]],
) -> tuple[ReconcileProgress, bool]:
    """Run the post-fan-out reconciliation phase. Called only when the gate has already passed.

    One connection for the whole checkpoint, but each primitive keeps its OWN
    ``with conn.begin():`` transaction (see ``indexer/store.py``) -- retired
    branches for every repo are reconciled first, then the repo-level purge is
    decided and (maybe) applied; the two are never wrapped in one shared outer
    transaction.

    Returns ``(progress, reconciliation_failed)``. On success ``progress``
    records what was committed; on a primitive raising mid-sequence, the ERROR
    is logged HERE (safe fields only: phase, repo, and the exception's type
    name -- never ``str(exc)`` or ``exc_info``, matching this module's existing
    redaction discipline) and ``reconciliation_failed=True`` is returned so
    ``run()`` can fail the process without re-deriving what happened.
    """
    progress = ReconcileProgress()
    desired_repos = sorted({o.name for o in repo_outcomes})
    phase = "retired-branches"
    repo_name = "-"
    try:
        with engine.connect() as conn:
            for outcome in repo_outcomes:
                repo_name = outcome.name
                persisted = {b for (n, b) in stamps if n == outcome.name.casefold()}
                resolved = {bo.branch for bo in outcome.outcomes}
                retired = sorted(persisted - resolved)
                if not retired:
                    continue
                counts = reconcile_retired_fn(conn, name=outcome.name, retired_branches=retired)
                # FIRST statement after the call returns -- see ReconcileProgress's docstring.
                progress.committed_any = True
                progress.branches_removed += counts.branches_removed
                progress.files_stripped += counts.files_stripped
                progress.files_deleted += counts.files_deleted

            phase = "removed-purge"
            repo_name = "-"
            # Deliberately UNFILTERED: victims are rows OUTSIDE this run's desired
            # set entirely, so a `.where(Repo.name.in_(...))` bound read (like
            # _read_stamps') would hide exactly the rows this guard exists to see.
            stored = set(conn.execute(select(Repo.name)).scalars().all())
            # This SELECT autobegins a transaction on `conn` (no prior statement in
            # this scope started one) that reconcile_removed_fn's own `with
            # conn.begin():` would otherwise collide with (SQLAlchemy raises
            # InvalidRequestError: a transaction is already begun on this
            # Connection) -- clear it first, matching the same idiom
            # tests/integration/test_reconcile.py uses after every read on a
            # connection a reconcile primitive will next call conn.begin() on.
            conn.rollback()
            would_purge = stored - set(desired_repos)
            progress.stored_count = len(stored)
            progress.would_purge_count = len(would_purge)
            if would_purge and len(would_purge) > MAX_PURGE_SHRINK_FRACTION * len(stored):
                # A withhold is a POLICY decision, not an exception -- log it here
                # (not in the except below) and leave committed_any/reconciliation_failed
                # alone; run() reads purge_blocked separately to fail the process.
                progress.purge_blocked = True
                logger.error(
                    "reconciliation withheld: purge would remove %d/%d stored repos, exceeding "
                    "the %.0f%% shrink guard -- check the GitHub token's org/repo scope before "
                    "assuming this is a legitimate mass decommission; stage repo removal across "
                    "multiple clean runs to purge more than half of the corpus at once "
                    "(retired-branch cleanup on survivors already committed: %d branch(es), "
                    "%d file(s) stripped, %d deleted)",
                    progress.would_purge_count,
                    progress.stored_count,
                    MAX_PURGE_SHRINK_FRACTION * 100,
                    progress.branches_removed,
                    progress.files_stripped,
                    progress.files_deleted,
                )
            else:
                purged = reconcile_removed_fn(conn, desired_repos=desired_repos)
                progress.committed_any = True
                progress.purged_repos = purged
    except Exception as exc:
        if progress.committed_any:
            logger.error(
                "corpus PARTIALLY reconciled before an error in the %s phase (repo=%s; %d "
                "branch(es) retired so far, %d repo(s) purged so far); remaining reconciliation "
                "completes on the next clean run (primitives are idempotent) [%s]",
                phase,
                repo_name,
                progress.branches_removed,
                len(progress.purged_repos),
                type(exc).__name__,
            )
        else:
            logger.error(
                "corpus left stale: no reconciliation committed (failed in the %s phase, "
                "repo=%s) [%s]",
                phase,
                repo_name,
                type(exc).__name__,
            )
        return progress, True
    return progress, False


def _precompute_chunk_writer(
    files: list[ParsedFile], embed_fn: EmbedFn, max_chunks_per_repo: int
) -> ChunkWriter:
    """Chunk + embed every file up front (issue #14 A4: no network inside conn.begin()).

    Returns a :data:`indexer.store.ChunkWriter` closure over the precomputed
    ``(chunk_index, content, start_line, end_line, embedding)`` tuples, keyed by
    file path, so ``index_repo`` can write them per file without ever calling the
    embedder itself. Raises ``ValueError`` if the repo's total chunk count exceeds
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

    by_path: dict[str, list[tuple[int, str, int, int, list[float]]]] = {}
    i = 0
    for path, chunks in per_file.items():
        if not chunks:
            continue
        by_path[path] = [
            (c.chunk_index, c.content, c.start_line, c.end_line, all_vectors[i + j])
            for j, c in enumerate(chunks)
        ]
        i += len(chunks)

    def chunk_writer(conn: Any, repo_id: int, file_id: int, pf: ParsedFile) -> None:
        write_chunks(conn, file_id=file_id, chunks=by_path.get(pf.path, []))

    return chunk_writer


def _index_one(
    entry: RepoEntry,
    *,
    http_client: httpx.Client,
    engine: Any,
    index_fn: Callable[..., IndexCounts],
    cfg: Settings,
    embed_fn: EmbedFn | None,
    stamps: dict[tuple[str, str], tuple[str | None, int | None]],
) -> RepoOutcome:
    """Run the full fetch -> parse -> symbols -> store pipeline for every branch of one repo.

    Branches are processed SEQUENTIALLY (plan Option A1) so no concurrent writer
    for this repo ever exists -- the invariant ``store.py``'s per-branch sweep
    depends on. Each branch's outcome is independent (see :class:`BranchOutcome`):
    one branch failing or conflicting does not stop this repo's other branches
    from being attempted.
    """
    started = time.monotonic()
    # Set FIRST, before normalize_repo, so a malformed entry's ValueError still
    # logs under the right name. The finally reset is mandatory: ThreadPoolExecutor
    # reuses worker threads and does NOT reset the context between tasks, so
    # without it a failure raised before the next set() logs under this repo.
    token = _repo_ctx.set(entry.name)
    try:
        return _index_one_inner(
            entry,
            started=started,
            http_client=http_client,
            engine=engine,
            index_fn=index_fn,
            cfg=cfg,
            embed_fn=embed_fn,
            stamps=stamps,
        )
    finally:
        _repo_ctx.reset(token)


def _index_one_inner(
    entry: RepoEntry,
    *,
    started: float,
    http_client: httpx.Client,
    engine: Any,
    index_fn: Callable[..., IndexCounts],
    cfg: Settings,
    embed_fn: EmbedFn | None,
    stamps: dict[tuple[str, str], tuple[str | None, int | None]],
) -> RepoOutcome:
    """The body of :func:`_index_one`, run with the repo log context already set.

    Resolving the default HEAD and the concrete branch list happens ONCE, here,
    outside the per-branch loop; a failure at this stage (a 404 repo, a rate
    limit) fails the whole repo and propagates to ``run()``'s repo-level
    handler -- there is no :class:`RepoOutcome` at all for that case (see its
    docstring). Everything from that point on is per-branch and never raises
    out of this function (see :func:`_index_one_branch`).
    """
    name = normalize_repo(entry.name)
    org, repo = name.split("/", 1)
    default_branch, default_head_sha = resolve_ref(http_client, org, repo)

    # An override matched to THIS repo by resolve_repos wins outright over the global
    # cap -- it is not a floor/ceiling blend, since a repo big enough to need one
    # override is usually big enough that the global default would just fail it again.
    max_chunks_per_repo = entry.semantic_max_chunks or cfg.semantic_max_chunks_per_repo
    if entry.semantic_max_chunks is not None:
        logger.info(
            "semantic chunk cap override for %s: %d (global %d)",
            name,
            entry.semantic_max_chunks,
            cfg.semantic_max_chunks_per_repo,
        )

    # The common case -- no branches: configured -- needs no GitHub branches API
    # call at all: resolve_branches ignores all_branches entirely when globs is
    # empty (it always resolves to just [default_branch]).
    all_branches = list_branches(http_client, org, repo) if entry.branch_globs else []
    resolution = resolve_branches(
        default_branch, all_branches, sorted(entry.branch_globs), repo=name
    )

    outcomes = [
        _index_one_branch(
            name,
            org=org,
            repo=repo,
            branch=branch,
            is_default=(branch == default_branch),
            default_head_sha=default_head_sha,
            http_client=http_client,
            engine=engine,
            index_fn=index_fn,
            cfg=cfg,
            embed_fn=embed_fn,
            stamps=stamps,
            started=started,
            max_chunks_per_repo=max_chunks_per_repo,
        )
        for branch in resolution.branches
    ]

    # Measured here, where the clock already runs, rather than as an IndexCounts
    # field: IndexCounts is a frozen dataclass compared by value in existing
    # assertions, and a nonzero-by-construction timing field would break them.
    # This is the instrument the "throughput measured on the first production
    # run" promise depends on -- without it that promise is unfalsifiable.
    logger.info("finished %s in %.2fs", name, time.monotonic() - started)
    return RepoOutcome(name=name, discovery_complete=resolution.complete, outcomes=outcomes)


def _index_one_branch(
    name: str,
    *,
    org: str,
    repo: str,
    branch: str,
    is_default: bool,
    default_head_sha: str,
    http_client: httpx.Client,
    engine: Any,
    index_fn: Callable[..., IndexCounts],
    cfg: Settings,
    embed_fn: EmbedFn | None,
    stamps: dict[tuple[str, str], tuple[str | None, int | None]],
    started: float,
    max_chunks_per_repo: int,
) -> BranchOutcome:
    """Fetch, parse, and store ONE branch. Never raises -- every failure is classified.

    ``stamps`` is looked up as ``(name.casefold(), branch)`` -- a miss (a branch
    never indexed before) degrades to "index it", safe in the correct direction.
    A stored ``None`` version means the provenance of the stored index is
    unknown, so the branch is always re-indexed.

    ``max_chunks_per_repo`` is the caller's (``_index_one_inner``'s) already-resolved
    effective cap -- this repo's ``semantic_max_chunks_per_repo`` override if one
    matched, else ``cfg.semantic_max_chunks_per_repo``. Taken as a parameter rather
    than read from ``cfg`` directly so every branch of a repo enforces the SAME
    resolved cap without recomputing (or risking drift on) the override lookup.
    """
    try:
        head_sha = (
            default_head_sha if is_default else resolve_branch_head(http_client, org, repo, branch)
        )

        # The skip seam: after the immutable HEAD SHA is known, before anything
        # is downloaded. Both halves must match -- a stored NULL version never does.
        stamp = stamps.get((name.casefold(), branch), (None, None))
        if stamp == (head_sha, INDEX_SEMANTICS_VERSION):
            logger.info(
                "skipped %s@%s: already indexed at %s (semantics v%d) in %.2fs",
                name,
                branch,
                head_sha,
                INDEX_SEMANTICS_VERSION,
                time.monotonic() - started,
            )
            return BranchOutcome(branch=branch, status="skipped")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Checked INSIDE the temp dir (so it measures the filesystem actually
            # being written to) and BEFORE the first byte is downloaded. Raising
            # here is caught below, costing this branch alone.
            assert_disk_headroom(tmp_path, repo=f"{name}@{branch}")
            tar_path = download_tarball(http_client, org, repo, head_sha, tmp_path)
            root = extract_tarball(tar_path, tmp_path / "extracted")

            chunk_writer: ChunkWriter | None = None
            if cfg.semantic_enabled and embed_fn is not None:
                # Chunking/embedding needs the full file list up front -- unlike
                # the lazy items generator below, it cannot stream through
                # index_repo's open transaction (A4).
                files = list(iter_source_files(root))
                try:
                    chunk_writer = _precompute_chunk_writer(files, embed_fn, max_chunks_per_repo)
                except Exception:
                    # The semantic layer is ADDITIVE: a chunk-ceiling breach, a downed embedder,
                    # or a dim/count mismatch must not cost this branch its core index. Letting
                    # it propagate would skip files/symbols AND the mark-and-sweep, silently
                    # leaving the branch stale -- worse than stale chunks. Chunks catch up on
                    # the next successful run; the failure is logged with a traceback, never
                    # swallowed silently.
                    logger.warning(
                        "semantic precompute failed for %s@%s; indexing core corpus without chunks",
                        name,
                        branch,
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
                    branch=branch,
                    is_default=is_default,
                    head_sha=head_sha,
                    items=items,
                    chunk_writer=chunk_writer,
                )
        return BranchOutcome(branch=branch, status="indexed", counts=counts)
    except StaleIndexError as exc:
        # The repo_branches row for THIS branch changed under this worker, so
        # its whole transaction rolled back and THIS BRANCH IS NOT INDEXED.
        #
        # NO KNOWN WRITER CAN REACH THIS TODAY -- do not go hunting for one.
        # index_repo's statements take row locks held to commit, so any
        # competing writer either blocks until this worker finishes or commits
        # first and this worker's baseline read then reads its value.
        #
        # It is excluded from the exit code because it is an invariant
        # assertion, not an expected failure path (see StaleIndexError in
        # indexer/store.py). It earns its keep by failing loudly if
        # for_each_task sharding lands, per-branch parallel fan-out (plan
        # Option A2) ships, or someone raises max_concurrent_runs -- any of
        # which removes the single-writer-per-repo property above.
        #
        # Should it ever fire, the branch self-heals: the next run sees a
        # stamp it does not match and re-indexes it.
        #
        # Logged WITHOUT a traceback: a rolled-back transaction is not a
        # crash, and the stack adds nothing an operator needs.
        logger.warning(
            "index conflict for %s@%s (rolled back, not indexed; will re-index next run): %s",
            name,
            branch,
            exc,
        )
        return BranchOutcome(branch=branch, status="conflict")
    except Exception:
        logger.exception("failed to index %s@%s", name, branch)
        return BranchOutcome(branch=branch, status="failed")


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
    # force=True is load-bearing, not boilerplate: basicConfig is a documented
    # no-op when the root logger ALREADY has handlers, and a serverless runtime
    # may well have configured logging before this entry point runs. Without it
    # the RepoLogFilter below still attaches -- but the pre-existing formatter
    # never references %(repo)s, so every line ships un-attributed under fan-out
    # and nothing errors. The test suite cannot catch this (it empties
    # root.handlers so basicConfig takes effect at all), so the guard has to be
    # here rather than in a test.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s [%(repo)s]: %(message)s",
        force=True,
    )
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
