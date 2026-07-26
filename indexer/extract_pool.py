"""Shared spawn-based process pool for symbol/edge extraction (issue #108).

``extract_file`` (``indexer.symbols``) is CPU-bound tree-sitter work that holds
the GIL through ``parse()``, so a repo's own worker THREAD (see ``indexer.job``)
cannot parallelize it. This module decouples extraction from that thread: one
:class:`ExtractionPool` is built once per :func:`indexer.job.run` and shared
across every repo worker, so a single giant repo's files parse on every
available core instead of one thread's worth.

**Not watched by the semantics tripwire, deliberately.** This module changes
*where* extraction runs, never *what* is extracted -- it imports and calls the
unmodified :func:`indexer.symbols.extract_file` for every file, so output is
identical by construction. It is therefore NOT added to
``tests/unit/test_semantics_version_tripwire.py``'s ``SEMANTICS_PATHS``: that
watch set is for modules that decide extraction *output*, not for a change in
execution topology. See ``indexer/AGENTS.md`` for the ``SEMANTICS_PATHS`` list.

Four load-bearing design decisions, each with a measured or reproduced failure
mode behind it (see the execution plan for the full writeup):

* **``spawn``, never ``fork``/``forkserver``.** ``ProcessPoolExecutor`` creates
  worker processes lazily on first ``submit()``, so building the pool early does
  NOT make ``fork`` safe -- the fork would still happen mid-run, from a process
  that by then holds a repo-worker ``ThreadPoolExecutor``, a live SQLAlchemy
  engine, an ``httpx.Client``, and (issue #107) a per-embed
  ``ThreadPoolExecutor``. Forking a threaded process inherits locks in
  indeterminate states -- the classic symptom is a silent stall, not a crash.
  ``spawn`` has no inherited-lock or inherited-fd hazard and is passed as an
  explicit ``multiprocessing.get_context("spawn")`` object; this module never
  calls ``multiprocessing.set_start_method`` (that mutates global interpreter
  state a Databricks ``python_wheel_task`` wrapper may depend on).
* **The preflight probe is a bare, terminable ``multiprocessing.Process``, run
  BEFORE any ``ProcessPoolExecutor`` exists -- never ``executor.submit()`` +
  ``future.result(timeout=...)``.** Reproduced: a timed-out task cannot be
  cancelled (``future.cancel()`` returns ``False``, already running), so
  ``executor.shutdown(wait=True)`` never returns, and even abandoning the
  executor still hangs the interpreter at exit (``concurrent.futures.process``
  registers an atexit hook that joins the executor manager thread). Since
  ``resources/job.yml`` sets ``max_concurrent_runs: 1`` with no
  ``timeout_seconds``, an executor-based probe that hangs does not degrade to
  "a WARNING and a run at today's speed" -- it blocks every queued run
  indefinitely until a human intervenes. A bare ``Process`` can be ``kill()``ed
  on a timeout, which an executor cannot offer.
* **A broken pool is a per-branch failure, not a run failure, and it
  self-heals.** ``ProcessPoolExecutor`` is permanently broken after any worker
  dies abnormally: every pending future *and every subsequent submit()* raises
  ``BrokenProcessPool``. Because the pool is shared, the true blast radius when
  a worker dies is "every repo worker holding a future at that moment" (up to
  ``effective_workers`` branches), not one -- state that honestly, not as "one
  branch". :class:`ExtractionPool` is a generation-tagged supervisor: it
  rebuilds the executor at most once per generation (compare-and-swap on the
  generation under a lock, so N concurrent branches hitting the same break
  trigger exactly one rebuild), bounded by :data:`MAX_POOL_REBUILDS`. Past that
  budget it latches to in-process extraction for the rest of the run and logs a
  WARNING -- slower, but correct and complete.
* **No prefetch thread, ever.** Since issue #106 the production file source is
  ``indexer.ingest.iter_tar_source_files``, a single forward pass over one open
  ``TarFile`` (``tf.members.clear()`` is the first statement of its loop body).
  :meth:`ExtractionPool.stream` pulls from that source strictly from the
  CALLING thread. A second thread advancing the same generator to "warm" the
  next batch would interleave ``tf.next()`` calls on a mid-stream ``TarFile``
  and corrupt output SILENTLY -- not as an exception. Bounded look-ahead
  (content-byte and file-count budgets scaled by ``n_processes``, covering
  BOTH submitted batches and locally-answered non-qualifying files) is
  achieved by pulling more items inside the consumer's own ``next()`` call,
  never concurrently.

No timing, no logging, and no DB/network access happens inside a worker: no
logger is configured in a spawned child (a stray ``logger.info`` there would
silently vanish), and ``indexer.timing``'s ambient ``ContextVar`` does not cross
a process boundary (its own module docstring names this issue by hand). Workers
are pure ``ParsedFile -> FileExtraction``.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import TYPE_CHECKING

from indexer.languages import SYMBOL_KINDS, FileExtraction, ParsedFile
from indexer.symbols import extract_file

if TYPE_CHECKING:
    from indexer.repo_config import RepoConfig

logger = logging.getLogger("indexer.extract_pool")

# Aggregate len(pf.content) per submitted batch -- deliberately NOT pf.size
# (indexer/store.py's write-batch bound): since #106 ParsedFile.size is the
# archive-declared length, which exceeds len(content) after NUL stripping.
# store.py bounds a WRITE payload, where the declared size is the honest bound;
# this bounds an IPC payload, which is the string actually pickled. Different
# quantities on purpose -- do not unify them.
_BATCH_BYTES = 2_000_000

# Backstop file-count bound, applied at TWO levels: (1) per submitted batch,
# alongside `_BATCH_BYTES` (whichever trips first), and (2) via
# `max_in_flight * _MAX_BATCH_FILES` on the overall look-ahead window -- both
# independent of `_BATCH_BYTES`. Zero- or near-zero-byte files (an empty
# `__init__.py`, a `.gitkeep`) barely move the byte bound, so a corpus
# dominated by them would otherwise defeat it and pickle one enormous batch --
# mirrors `indexer/store.py`'s own dual byte-and-count bound
# (`_BATCH_MAX_CONTENT_BYTES` / `_BATCH_MAX_FILES`) for the same reason.
_MAX_BATCH_FILES = 2000

# Bare-Process preflight probe timeout. Generous on purpose: a healthy runtime
# answers in well under a second (measured ~0.06s); this bound exists only to
# cap the pathological "sandbox silently never reports back" case, and it is
# fully recoverable (kill + latch) either way.
_PROBE_TIMEOUT_S = 60.0

# Rebuild-once-per-generation budget for a shared pool that keeps breaking
# (a deterministically poisoned file, a flaky sandbox). Past this, further
# rebuild attempts buy nothing -- latch to in-process and finish the run.
MAX_POOL_REBUILDS = 3

_PROBE_SENTINEL = "ok"


def _needs_parse(pf: ParsedFile) -> bool:
    """Would ``extract_file(pf)`` do any real work, or short-circuit to empty?

    Mirrors ``extract_file``'s own short-circuit (``pf.lang is None`` or the
    language has no ``SYMBOL_KINDS`` entry) so the pooled path can answer a
    guaranteed-empty file locally instead of paying IPC for it. The two must
    stay in exact agreement -- pinned directly by a test that checks every
    language in ``EXT_TO_LANG.values()`` plus ``None`` plus a bogus value.
    """
    return pf.lang is not None and pf.lang in SYMBOL_KINDS


def _extract_batch(batch: list[ParsedFile]) -> list[FileExtraction]:
    """Extract every file in ``batch``, in order. Module-level so it is picklable
    by qualified name under ``spawn`` -- calls the SAME ``extract_file`` the
    in-process path calls, so parity is structural, not re-implemented."""
    return [extract_file(pf) for pf in batch]


def _available_cpus() -> int:
    """Usable CPU count: affinity-aware, cgroup-v2-quota-aware, floor of 1.

    Prefers ``os.sched_getaffinity(0)`` over ``os.cpu_count()`` (affinity
    respects cpuset pinning; ``cpu_count`` reports host cores), then clamps by
    the cgroup-v2 CPU quota when ``/sys/fs/cgroup/cpu.max`` is readable and
    parses to a smaller number. Every branch here is pure and unit-tested
    against synthetic inputs -- this function's REAL return value is never
    asserted on in a test (it depends on the test host).
    """
    getter = getattr(os, "sched_getaffinity", None)
    n = len(getter(0)) if getter is not None else (os.cpu_count() or 1)
    quota = _cgroup_cpu_quota()
    if quota is not None:
        n = min(n, quota)
    return max(1, n)


def _cgroup_cpu_quota() -> int | None:
    """Parse ``/sys/fs/cgroup/cpu.max`` ("$QUOTA $PERIOD" or "max $PERIOD").

    Returns ``None`` (no clamp) for an unreadable file, an unlimited quota
    (``"max"``), or a malformed line -- safe in the correct direction, since
    the caller's own ``min(..., 8)`` ceiling still bounds the result.
    """
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text()
    except OSError:
        return None
    parts = raw.split()
    if len(parts) != 2:
        return None
    quota_str, period_str = parts
    if quota_str == "max":
        return None
    try:
        quota = int(quota_str)
        period = int(period_str)
    except ValueError:
        return None
    if period <= 0:
        return None
    return max(1, quota // period)


def derive_process_count(config: RepoConfig) -> int:
    """Worker-process count for the shared extraction pool.

    ``config.extract_processes`` set explicitly wins outright (``1`` is the
    kill switch -- see :func:`build_extraction_pool`). Unset (``None``, the
    default) derives from the runtime: affinity/cgroup-aware CPU count, capped
    at 8 to match every other parallelism knob in this repo
    (``index_concurrency``, ``SemanticOverrides.embedding_concurrency``).
    """
    if config.extract_processes is not None:
        return config.extract_processes
    return min(_available_cpus(), 8)


class ExtractionPoolError(RuntimeError):
    """A shared extraction pool broke while processing a batch.

    Always chained ``from`` the underlying ``BrokenProcessPool`` and always
    fails the branch that raised it -- caught by
    ``indexer.job._index_one_branch``'s existing broad ``except Exception``,
    exactly like any other extraction failure. The pool itself has already
    started (or exhausted its budget for) a rebuild by the time this is
    raised; the NEXT branch to call :meth:`ExtractionPool.stream` sees the
    rebuilt (or latched-to-in-process) pool, not this one.
    """


def _probe_child(queue: "multiprocessing.Queue[str]") -> None:
    """Preflight probe worker: exercise the real per-worker import + extraction
    path (grammar load included), then report success. Never returns data other
    than the bare sentinel -- see :func:`_run_preflight_probe` for why."""
    pf = ParsedFile(path="__preflight__.py", lang="python", size=0, content="def f():\n    pass\n")
    extract_file(pf)
    queue.put(_PROBE_SENTINEL)


def _drain(queue: "multiprocessing.Queue[str]") -> str | None:
    """Best-effort, non-blocking read of the probe's sentinel. ``None`` on any
    failure (empty queue, a queue whose feeder thread never flushed) -- the
    caller already knows the child exited 0 by this point; this is corroboration,
    not the primary signal."""
    try:
        return queue.get_nowait()  # type: ignore[no-any-return]
    except Exception:
        return None


def _run_preflight_probe(
    *,
    target: "Callable[[multiprocessing.Queue[str]], None]" = _probe_child,
    timeout: float = _PROBE_TIMEOUT_S,
) -> bool:
    """Verify ``spawn``-based multiprocessing actually works here, without ever
    constructing a ``ProcessPoolExecutor``.

    A bare, terminable ``multiprocessing.Process`` -- see the module docstring
    for why an executor-based probe is actively worse than skipping the probe
    entirely. ``join(timeout=...)`` rather than a blocking ``queue.get(timeout=
    ...)`` is deliberate: a crashed child is detected the instant it exits,
    rather than burning the full timeout waiting on a queue that will never
    receive anything.

    ``target``/``timeout`` are test seams only (mirroring this repo's injected-
    callable convention -- see ``indexer/AGENTS.md``); production always calls
    this with both defaults. Both must be picklable by qualified name under
    ``spawn``, so a test override must be a MODULE-LEVEL function, never a
    closure or a lambda.

    Every failure mode is caught under a bare ``except Exception``, not a type
    list: an unguarded ``__main__`` under ``spawn`` (the serverless hazard this
    probe exists to catch -- see the module docstring and ``docs/runbooks``)
    makes ``ctx.Process(...).start()`` raise ``RuntimeError`` from
    ``multiprocessing.spawn._check_not_importing_main`` in THIS process, not
    ``OSError`` -- narrower exception handling here would let that one escape
    to the caller instead of latching to in-process extraction.
    """
    ctx = multiprocessing.get_context("spawn")
    try:
        queue: "multiprocessing.Queue[str]" = ctx.Queue()
    except Exception:
        logger.warning(
            "extraction pool preflight failed: could not create an IPC queue "
            "(sem_open); falling back to in-process extraction for this run",
            exc_info=True,
        )
        return False

    process = ctx.Process(target=target, args=(queue,), daemon=True)
    try:
        process.start()
    except Exception:
        logger.warning(
            "extraction pool preflight failed: could not start a worker process; "
            "falling back to in-process extraction for this run",
            exc_info=True,
        )
        return False

    process.join(timeout=timeout)
    if process.is_alive():
        # The disposal an executor cannot offer: kill, then join to reap it.
        process.kill()
        process.join()
        logger.warning(
            "extraction pool preflight failed: probe worker did not respond "
            "within %.0fs (killed); falling back to in-process extraction for "
            "this run",
            timeout,
        )
        return False

    if process.exitcode != 0:
        logger.warning(
            "extraction pool preflight failed: probe worker exited with code %s; "
            "falling back to in-process extraction for this run",
            process.exitcode,
        )
        return False

    if _drain(queue) != _PROBE_SENTINEL:
        logger.warning(
            "extraction pool preflight failed: probe worker exited cleanly but "
            "never reported success (missing sentinel); falling back to "
            "in-process extraction for this run",
        )
        return False

    return True


class _FutureHolder:
    """One submitted batch's shared future, referenced by every slot in it."""

    __slots__ = ("future",)

    def __init__(self, future: "Future[list[FileExtraction]]") -> None:
        self.future = future


class _Slot:
    """One file's position in the output stream: either already answered
    locally (``local`` set), or awaiting its batch's shared future (``holder``
    + its index within that batch's result list)."""

    __slots__ = ("pf", "local", "holder", "index")

    def __init__(self, pf: ParsedFile, local: FileExtraction | None = None) -> None:
        self.pf = pf
        self.local = local
        self.holder: _FutureHolder | None = None
        self.index = 0


class ExtractionPool:
    """Supervises a shared, ``spawn``-based extraction pool for one run.

    Owns ``(executor, generation)`` under a lock (D7's supervisor). Built once
    per run in ``indexer.job.run`` and shared across every repo worker thread;
    ``submit()`` from N concurrent threads into one ``ProcessPoolExecutor`` is
    supported (the executor is internally locked). ``n_processes < 2`` means no
    executor is ever built -- :meth:`stream` degrades to plain in-process
    extraction, exactly today's expression.
    """

    def __init__(self, n_processes: int, *, batch_bytes: int = _BATCH_BYTES) -> None:
        self._n_processes = n_processes
        self._batch_bytes = batch_bytes
        self._lock = threading.Lock()
        self._generation = 0
        self._rebuilds = 0
        self._latched = False
        self._executor: ProcessPoolExecutor | None = (
            self._new_executor() if n_processes >= 2 else None
        )

    def _new_executor(self) -> ProcessPoolExecutor:
        ctx = multiprocessing.get_context("spawn")
        return ProcessPoolExecutor(max_workers=self._n_processes, mp_context=ctx)

    def _current(self) -> tuple[ProcessPoolExecutor | None, int]:
        with self._lock:
            return self._executor, self._generation

    def _handle_broken(self, generation: int) -> None:
        """React to a ``BrokenProcessPool`` observed against ``generation``.

        Compare-and-swap on the generation, under the lock, so N concurrent
        callers hitting the same break trigger exactly one rebuild (or one
        latch): the first caller in advances the generation (or latches); every
        other caller sees ``generation != self._generation`` and no-ops. The
        caller always raises :class:`ExtractionPoolError` regardless of what
        happens here -- this method only updates state for the NEXT call to
        :meth:`stream`.
        """
        old_executor: ProcessPoolExecutor | None = None
        with self._lock:
            if self._latched or generation != self._generation:
                pass
            else:
                old_executor = self._executor
                if self._rebuilds >= MAX_POOL_REBUILDS:
                    self._latched = True
                    self._executor = None
                    logger.warning(
                        "extraction pool: rebuild budget (%d) exhausted after "
                        "repeated BrokenProcessPool; latching to in-process "
                        "extraction for the rest of this run (rollback: set "
                        "extract_processes: 1 in config.yaml)",
                        MAX_POOL_REBUILDS,
                    )
                else:
                    self._rebuilds += 1
                    self._generation += 1
                    self._executor = self._new_executor()
                    logger.warning(
                        "extraction pool: a worker died (BrokenProcessPool); "
                        "rebuilt the pool (generation %d, rebuild %d/%d)",
                        self._generation,
                        self._rebuilds,
                        MAX_POOL_REBUILDS,
                    )
        # Disposed OUTSIDE the lock, and only with plain shutdown(wait=False):
        # a BROKEN executor's manager thread has already terminated, so this
        # cannot hang (unlike the healthy-executor teardown in shutdown()).
        if old_executor is not None:
            old_executor.shutdown(wait=False)

    def stream(self, files: Iterable[ParsedFile]) -> Iterator[tuple[ParsedFile, FileExtraction]]:
        """Yield ``(pf, FileExtraction)`` pairs for every file in ``files``, in
        source order.

        Pull-driven, strictly from the calling thread: ``files`` (in production,
        ``indexer.ingest.iter_tar_source_files``'s single-pass generator) is
        never advanced from any other thread. Qualifying files (see
        :func:`_needs_parse`) are accumulated into batches bounded by
        ``batch_bytes`` of aggregate ``len(pf.content)`` and submitted to the
        pool; non-qualifying files are answered locally with
        ``FileExtraction([], [])`` and interleaved back at their original
        position. Look-ahead is bounded by TWO independent budgets scaled by
        ``2 * n_processes`` -- aggregate pending content bytes, and pending
        file count (a backstop for a corpus of near-zero-byte files, which the
        byte budget alone would not throttle) -- covering both submitted
        batches and locally-answered files; pulling more is throttled by
        consuming (blocking on) the oldest batch first.

        Raises :class:`ExtractionPoolError` (chained from the underlying
        ``BrokenProcessPool``) the first time a submitted batch's worker turns
        out to have died -- the caller (one repo branch) fails; every future
        call to :meth:`stream` sees whatever :meth:`_handle_broken` decided.
        """
        executor, generation = self._current()
        if executor is None:
            for pf in files:
                yield pf, extract_file(pf)
            return
        yield from self._stream_pooled(files, executor, generation)

    def _stream_pooled(
        self,
        files: Iterable[ParsedFile],
        executor: ProcessPoolExecutor,
        generation: int,
    ) -> Iterator[tuple[ParsedFile, FileExtraction]]:
        # Bounds the total content bytes pulled-but-not-yet-yielded, across
        # BOTH local (non-qualifying) and batched slots -- not "in-flight
        # batch count". A corpus dominated by non-qualifying files (markdown,
        # data, unknown extensions) never submits a batch at all, so a count
        # of in-flight batches alone would never throttle it and `pending`
        # would grow to the full corpus. Same total budget as a pure
        # batch-count scheme (`max_in_flight` batches of up to `_batch_bytes`
        # each), just measured directly in bytes so it also covers the
        # local-only case.
        #
        # `max_pending_slots` is a second, independent bound on FILE COUNT:
        # zero- or near-zero-byte files (an empty `__init__.py`, a `.gitkeep`)
        # contribute ~nothing to `pending_bytes`, so the byte bound alone never
        # throttles a corpus dominated by them. Mirrors `indexer/store.py`'s
        # own dual byte-and-count bound (`_BATCH_MAX_CONTENT_BYTES` /
        # `_BATCH_MAX_FILES`) for the same reason: whichever limit is tighter
        # for a given corpus shape is the one that actually binds.
        max_in_flight = max(1, 2 * self._n_processes)
        max_pending_bytes = max_in_flight * self._batch_bytes
        max_pending_slots = max_in_flight * _MAX_BATCH_FILES
        source = iter(files)
        pending: deque[_Slot] = deque()
        in_flight: deque[_FutureHolder] = deque()
        pending_bytes = 0
        batch_files: list[ParsedFile] = []
        batch_slots: list[_Slot] = []
        batch_bytes = 0
        exhausted = False

        def flush_batch() -> None:
            nonlocal batch_files, batch_slots, batch_bytes
            if not batch_files:
                return
            try:
                future = executor.submit(_extract_batch, batch_files)
            except BrokenProcessPool as exc:
                self._handle_broken(generation)
                raise ExtractionPoolError(
                    f"shared extraction pool (generation {generation}) broke while "
                    "submitting a batch -- a worker likely crashed or was OOM-killed. "
                    "This branch failed and will re-index on the next run. Rollback: "
                    "set extract_processes: 1 in config.yaml to disable pooled "
                    "extraction."
                ) from exc
            holder = _FutureHolder(future)
            for i, slot in enumerate(batch_slots):
                slot.holder = holder
                slot.index = i
            in_flight.append(holder)
            batch_files = []
            batch_slots = []
            batch_bytes = 0

        def pull_one() -> bool:
            """Pull exactly one item from ``source``. ``False`` at EOF (which
            also flushes any still-accumulating batch, so nothing is stranded
            unsubmitted when the source runs dry)."""
            nonlocal batch_bytes, pending_bytes, exhausted
            try:
                pf = next(source)
            except StopIteration:
                exhausted = True
                flush_batch()
                return False
            pending_bytes += len(pf.content)
            if not _needs_parse(pf):
                pending.append(_Slot(pf, local=FileExtraction(symbols=[], edges=[])))
                return True
            slot = _Slot(pf)
            pending.append(slot)
            batch_files.append(pf)
            batch_slots.append(slot)
            batch_bytes += len(pf.content)
            if batch_bytes >= self._batch_bytes or len(batch_files) >= _MAX_BATCH_FILES:
                flush_batch()
            return True

        try:
            while True:
                while (
                    not exhausted
                    and pending_bytes < max_pending_bytes
                    and len(pending) < max_pending_slots
                ):
                    pull_one()
                # Force out a still-accumulating partial batch ONLY when it is
                # about to be popped next (its first slot is the front of
                # `pending`): a long run of non-qualifying (local) files pulled
                # AFTER this batch's own files can exhaust the budget while the
                # batch itself is still under its own _batch_bytes threshold,
                # and since the batch's slots were pulled BEFORE those local
                # files, they sit AHEAD of them in `pending` -- reaching the
                # front, unflushed, before the local files do. Guarding on
                # "is it actually the front" (rather than flushing
                # unconditionally every outer-loop iteration) keeps a batch
                # accumulating normally toward its full _batch_bytes whenever
                # something else is ahead of it in the queue, so this does not
                # fragment steady-state batching into one-file submissions.
                if pending and pending[0].local is None and pending[0].holder is None:
                    flush_batch()
                if not pending:
                    return

                slot = pending.popleft()
                pending_bytes -= len(slot.pf.content)
                if slot.local is not None:
                    yield slot.pf, slot.local
                    continue

                holder = slot.holder
                # Invariant: every non-local slot has a holder by this point,
                # because the unconditional flush_batch() call directly above
                # empties `batch_files`/`batch_slots` before we ever pop from
                # `pending` -- there is never an unflushed batch slot inside
                # `pending` at the moment we reach this line.
                assert holder is not None, "unflushed batch slot reached the front of pending"
                try:
                    results = holder.future.result()
                except BrokenProcessPool as exc:
                    self._handle_broken(generation)
                    raise ExtractionPoolError(
                        f"shared extraction pool (generation {generation}) broke while "
                        "processing a batch -- a worker likely crashed or was OOM-killed. "
                        "This branch failed and will re-index on the next run. Rollback: "
                        "set extract_processes: 1 in config.yaml to disable pooled "
                        "extraction."
                    ) from exc
                finally:
                    if in_flight and in_flight[0] is holder:
                        in_flight.popleft()
                yield slot.pf, results[slot.index]
        finally:
            # Cleanup on abandonment: `files` is consumed inside index_repo's
            # open transaction, so any exception in that loop (a DB error,
            # StaleIndexError, a chunk_writer failure) abandons this generator
            # with however many batches the look-ahead budgets above still
            # left submitted and unconsumed. Cancel every
            # one we haven't consumed -- a future already running cannot be
            # cancelled (returns False) but finishes in milliseconds and its
            # discarded result costs nothing.
            for holder in in_flight:
                holder.future.cancel()

    def shutdown(self) -> None:
        """Tear down the executor, if one exists. ``wait=True``, and safe to
        call unconditionally: by the time ``indexer.job.run`` reaches its
        ``finally``, the repo-worker ``ThreadPoolExecutor``'s own ``with`` block
        has already exited (every branch joined), so no branch holds a future
        against this pool -- the one condition that makes ``wait=True`` safe
        (see the module's execution-plan writeup). Never
        ``cancel_futures=True``, matching this repo's existing executor-teardown
        discipline (``indexer/job.py``).
        """
        with self._lock:
            executor = self._executor
            self._executor = None
            self._latched = True
        if executor is not None:
            executor.shutdown(wait=True)


def build_extraction_pool(config: RepoConfig) -> ExtractionPool:
    """Derive this run's process count, preflight it, and build the pool.

    ``extract_processes: 1`` (explicit or, on a single-core host, derived) is
    the kill switch: no preflight probe runs and no executor is ever built --
    :meth:`ExtractionPool.stream` degrades straight to in-process extraction.
    For ``>= 2``, the preflight probe (see :func:`_run_preflight_probe`) MUST
    pass before any ``ProcessPoolExecutor`` is constructed; a failing probe logs
    a WARNING and returns an already-latched, in-process-only pool instead of
    raising -- a degraded run at today's speed, never a failed one.
    """
    n = derive_process_count(config)
    if n < 2:
        logger.info("symbol extraction: in-process (extract_processes=1; no pool built)")
        return ExtractionPool(n_processes=0)
    if not _run_preflight_probe():
        # _run_preflight_probe already logged the WARNING naming the cause.
        return ExtractionPool(n_processes=0)
    logger.info("symbol extraction: %d process(es) (spawn); pool preflight ok", n)
    return ExtractionPool(n_processes=n)
