"""Unit tests for indexer.extract_pool: the shared process-pool extraction (#108).

T1-T8 below mirror the execution plan's numbering. T9/T10 (the existing
``test_job.py`` suite's kill-switch pin and phase-timing attribution) live in
``tests/unit/test_job.py`` -- this file covers ``indexer.extract_pool`` in
isolation.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future
from typing import Any

import pytest

from indexer import extract_pool
from indexer.extract_pool import (
    _MAX_BATCH_FILES,
    MAX_POOL_REBUILDS,
    ExtractionPool,
    ExtractionPoolError,
    _needs_parse,
    build_extraction_pool,
    derive_process_count,
)
from indexer.languages import EXT_TO_LANG, SYMBOL_KINDS, FileExtraction, ParsedFile
from indexer.repo_config import RepoConfig
from indexer.symbols import extract_file

# --- fixtures -----------------------------------------------------------------

# One real per-language snippet each (reusing test_symbols.py's shapes), so T1's
# parity check exercises every SYMBOL_KINDS language through a real spawn pool.
_LANG_SOURCES: dict[str, str] = {
    "python": "class C:\n    def m(self):\n        pass\ndef top():\n    pass\n",
    "javascript": "class C {\n  m() {}\n}\nfunction top() {}\n",
    "typescript": "interface I {}\nclass C {\n  m() {}\n}\nfunction top() {}\n",
    "tsx": "function top() {}\n",
    "go": "package main\nfunc top() {}\nfunc (r R) m() {}\ntype T int\n",
    "java": "class C {\n  void m() {}\n}\ninterface I {}\n",
    "rust": "fn top() {}\nstruct S;\nenum E {}\ntrait T {}\n",
}


def _fixture_corpus() -> list[ParsedFile]:
    files = [
        ParsedFile(path=f"src/f.{lang}", lang=lang, size=len(content), content=content)
        for lang, content in _LANG_SOURCES.items()
    ]
    files.append(ParsedFile(path="unknown.dat", lang=None, size=3, content="hi\n"))
    files.append(ParsedFile(path="doc.md", lang="markdown", size=5, content="# hi\n"))
    files.append(ParsedFile(path="empty.py", lang="python", size=0, content=""))
    files.append(
        ParsedFile(
            path="nested.py",
            lang="python",
            size=0,
            content=(
                "class Outer:\n"
                "    def method(self):\n"
                "        return helper()\n"
                "def helper():\n"
                "    return 1\n"
            ),
        )
    )
    return files


def _python_files(n: int, *, size: int = 200) -> list[ParsedFile]:
    padding = "x" * max(size - 20, 1)
    return [
        ParsedFile(
            path=f"pkg/mod{i}.py",
            lang="python",
            size=size,
            content=f"def f{i}():\n    return '{padding}'\n",
        )
        for i in range(n)
    ]


def _config(*, extract_processes: int | None = None) -> RepoConfig:
    doc: dict[str, Any] = {"version": 1, "connections": [{"type": "github", "users": ["u"]}]}
    if extract_processes is not None:
        doc["extract_processes"] = extract_processes
    return RepoConfig.model_validate(doc)


# Module-level (picklable under spawn) preflight-probe targets for T6a/T6b --
# see _run_preflight_probe's docstring: a test override must be a module-level
# function, never a closure or lambda, or spawn's re-import cannot find it.
def _crashing_probe_target(queue: Any) -> None:
    raise RuntimeError("simulated preflight crash")


def _hanging_probe_target(queue: Any) -> None:
    time.sleep(3600)


# --- T1: parity, real spawn pool ----------------------------------------------


@pytest.mark.unit
def test_stream_parity_against_extract_file_real_spawn_pool() -> None:
    """T1 (AC2): real spawn processes, real pickling -- the risk the issue names.

    ``max_workers=2`` is pinned deliberately: each spawned child re-imports
    ``tree_sitter_language_pack``, so worker count drives this test's cost more
    than corpus size does. A tiny ``batch_bytes`` forces the fixture corpus
    (well under the real 2 MB default) across >=3 batches, so submit/collect
    round-trips more than once.
    """
    files = _fixture_corpus()
    pool = ExtractionPool(n_processes=2, batch_bytes=20)
    try:
        got = list(pool.stream(files))
    finally:
        pool.shutdown()

    expected = [(pf, extract_file(pf)) for pf in files]
    assert got == expected


@pytest.mark.unit
def test_build_extraction_pool_real_spawn_end_to_end() -> None:
    """Sanity: the full build_extraction_pool -> preflight -> stream wiring,
    through a real spawn pool (not the lower-level ExtractionPool constructor
    T1 uses directly)."""
    pool = build_extraction_pool(_config(extract_processes=2))
    try:
        assert pool._executor is not None
        files = _python_files(5)
        got = list(pool.stream(files))
    finally:
        pool.shutdown()

    expected = [(pf, extract_file(pf)) for pf in files]
    assert got == expected


# --- T2: routing-predicate agreement ------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("lang", [*sorted(EXT_TO_LANG.values()), None, "bogus"])
def test_needs_parse_agrees_with_extract_file_short_circuit(lang: str | None) -> None:
    pf = ParsedFile(path="x", lang=lang, size=0, content="whatever\n")
    expect_parse = lang is not None and lang in SYMBOL_KINDS
    assert _needs_parse(pf) is expect_parse
    if not expect_parse:
        assert extract_file(pf) == FileExtraction(symbols=[], edges=[])


# --- T3: bounded look-ahead ----------------------------------------------------


class _CountingSource:
    """Wraps a file list, counting how many items have actually been pulled."""

    def __init__(self, files: list[ParsedFile]) -> None:
        self._it = iter(files)
        self.pulled = 0

    def __iter__(self) -> "_CountingSource":
        return self

    def __next__(self) -> ParsedFile:
        pf = next(self._it)
        self.pulled += 1
        return pf


class _ImmediateFuture:
    def __init__(self, result: list[FileExtraction]) -> None:
        self._result = result

    def result(self) -> list[FileExtraction]:
        return self._result

    def cancel(self) -> bool:
        return False


class _ImmediateExecutor:
    """Resolves every submitted batch synchronously -- still lets ExtractionPool's
    own in_flight bookkeeping (not completion timing) drive the look-ahead bound."""

    def submit(self, fn: Any, *args: Any) -> _ImmediateFuture:
        return _ImmediateFuture(fn(*args))


@pytest.mark.unit
def test_bounded_look_ahead() -> None:
    """T3: the issue's explicit no-full-materialization rule. Each file alone
    exceeds the tiny batch_bytes, so one file == one batch, and max_in_flight
    (2 * n_processes) directly bounds how many files are pulled ahead."""
    total = 20
    files = _python_files(total, size=200)
    source = _CountingSource(files)

    pool = ExtractionPool(n_processes=0, batch_bytes=50)
    pool._n_processes = 2
    pool._executor = _ImmediateExecutor()  # type: ignore[assignment]
    max_in_flight = 2 * pool._n_processes

    gen = pool.stream(source)  # type: ignore[arg-type]
    next(gen)
    assert source.pulled <= max_in_flight
    assert source.pulled < total

    for _ in gen:
        assert source.pulled <= total
    assert source.pulled == total


@pytest.mark.unit
def test_bounded_look_ahead_for_non_qualifying_files() -> None:
    """Regression: non-qualifying (locally-answered) files never enter a batch,
    so a look-ahead bound keyed on in-flight BATCH count alone never throttles a
    corpus dominated by them -- the whole source would be pulled in one shot.
    The bound must cover pending local content bytes too."""
    total = 500
    files = [
        ParsedFile(path=f"doc{i}.md", lang=None, size=2000, content="x" * 2000)
        for i in range(total)
    ]
    source = _CountingSource(files)

    # batch_bytes deliberately tiny: max_pending_bytes (4 * batch_bytes) must be
    # well under the corpus's total bytes (500 * 2000 = 1,000,000) for this test
    # to actually exercise the throttle rather than trivially fitting everything
    # under budget.
    pool = ExtractionPool(n_processes=0, batch_bytes=1000)
    pool._n_processes = 2
    pool._executor = _ImmediateExecutor()  # type: ignore[assignment]

    gen = pool.stream(source)  # type: ignore[arg-type]
    next(gen)
    assert source.pulled < total  # bounded -- NOT the whole corpus in one shot

    for _ in gen:
        pass
    assert source.pulled == total


class _RecordingExecutor:
    """Resolves synchronously like _ImmediateExecutor, but also records each
    submitted batch's file count -- for pinning the per-batch file-count cap."""

    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def submit(self, fn: Any, *args: Any) -> _ImmediateFuture:
        self.batch_sizes.append(len(args[0]))
        return _ImmediateFuture(fn(*args))


@pytest.mark.unit
def test_batch_is_capped_by_file_count_even_with_huge_batch_bytes() -> None:
    """Regression: a corpus of near-zero-byte qualifying files barely moves
    batch_bytes, so without a per-batch FILE-COUNT cap a single batch would grow
    to the entire look-ahead window (thousands of files, one oversized pickle on
    one idle worker) before ever tripping the byte bound."""
    total = _MAX_BATCH_FILES * 2 + 5
    files = _python_files(total, size=1)  # near-zero content per file
    executor = _RecordingExecutor()

    pool = ExtractionPool(n_processes=0, batch_bytes=10_000_000)  # never trips
    pool._n_processes = 2
    pool._executor = executor  # type: ignore[assignment]

    got = list(pool.stream(files))
    assert got == [(pf, extract_file(pf)) for pf in files]
    assert executor.batch_sizes  # at least one batch was actually submitted
    assert max(executor.batch_sizes) <= _MAX_BATCH_FILES


# --- T4: order preservation under out-of-order completion ---------------------


class _ReverseOrderExecutor:
    """Batch N's future resolves BEFORE batch N-1's -- proves stream() yields in
    submission (source) order, not completion order."""

    def __init__(self, total_batches: int) -> None:
        self._total = total_batches
        self._n = 0

    def submit(self, fn: Any, *args: Any) -> "Future[list[FileExtraction]]":
        self._n += 1
        idx = self._n
        fut: "Future[list[FileExtraction]]" = Future()

        def _worker() -> None:
            time.sleep(0.01 * (self._total - idx + 1))
            fut.set_result(fn(*args))

        threading.Thread(target=_worker, daemon=True).start()
        return fut


@pytest.mark.unit
def test_order_preservation_under_out_of_order_completion() -> None:
    """T4 (D6): later-submitted batches resolving first must not reorder output."""
    files = _python_files(6, size=200)  # tiny batch_bytes -> one batch per file
    pool = ExtractionPool(n_processes=0, batch_bytes=50)
    pool._n_processes = 2
    pool._executor = _ReverseOrderExecutor(len(files))  # type: ignore[assignment]

    got = list(pool.stream(files))
    expected = [(pf, extract_file(pf)) for pf in files]
    assert got == expected


# --- T5: BrokenProcessPool isolation, two concurrent streams ------------------


class _BrokenFuture:
    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self._barrier = barrier

    def result(self) -> list[FileExtraction]:
        if self._barrier is not None:
            # Rendezvous so two concurrent callers genuinely observe the SAME
            # (pre-rebuild) executor before either raises -- without this, one
            # thread can race ahead, rebuild, and hand the second thread an
            # already-healthy pool, which would defeat the point of this test.
            self._barrier.wait(timeout=10.0)
        raise extract_pool.BrokenProcessPool("worker died")

    def cancel(self) -> bool:
        return False


class _AlwaysBreakingExecutor:
    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self._barrier = barrier

    def submit(self, fn: Any, *args: Any) -> _BrokenFuture:
        return _BrokenFuture(self._barrier)

    def shutdown(self, wait: bool = True) -> None:
        pass


class _WorkingExecutor:
    def submit(self, fn: Any, *args: Any) -> "Future[list[FileExtraction]]":
        fut: "Future[list[FileExtraction]]" = Future()
        fut.set_result(fn(*args))
        return fut

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.mark.unit
def test_broken_process_pool_isolation_two_concurrent_streams() -> None:
    """T5 (AC3): the two-thread shape is the point -- a single-stream test would
    pass against a racy rebuild."""
    files = _python_files(3, size=50)
    pool = ExtractionPool(n_processes=0, batch_bytes=10)
    pool._n_processes = 2
    barrier = threading.Barrier(2)
    pool._executor = _AlwaysBreakingExecutor(barrier=barrier)  # type: ignore[assignment]

    rebuild_calls = {"n": 0}

    def _new_executor_stub() -> Any:
        rebuild_calls["n"] += 1
        # The FIRST rebuild recovers onto a healthy executor (proves "a
        # subsequent stream succeeds on the new generation"); every rebuild
        # after that keeps breaking, driving toward the rebuild budget.
        return _WorkingExecutor() if rebuild_calls["n"] == 1 else _AlwaysBreakingExecutor()

    pool._new_executor = _new_executor_stub  # type: ignore[method-assign]

    errors: list[ExtractionPoolError] = []
    lock = threading.Lock()

    def _drive() -> None:
        try:
            list(pool.stream(files))
        except ExtractionPoolError as exc:
            with lock:
                errors.append(exc)

    t1 = threading.Thread(target=_drive)
    t2 = threading.Thread(target=_drive)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 2  # both concurrent callers saw the failure
    assert isinstance(errors[0].__cause__, extract_pool.BrokenProcessPool)
    assert "extract_processes: 1" in str(errors[0])  # names the rollback
    assert pool._rebuilds == 1  # exactly one rebuild for N concurrent breaks
    assert pool._generation == 1
    assert not pool._latched

    # A subsequent stream succeeds on the new (healthy) generation.
    got = list(pool.stream(files))
    assert got == [(pf, extract_file(pf)) for pf in files]

    # Simulate a later worker death under the SAME (still healthy) generation,
    # driving repeated rebuilds until the budget is exhausted and it latches.
    pool._executor = _AlwaysBreakingExecutor()  # type: ignore[assignment]
    for _ in range(MAX_POOL_REBUILDS - 1):
        with pytest.raises(ExtractionPoolError):
            list(pool.stream(files))
    assert pool._rebuilds == MAX_POOL_REBUILDS
    assert not pool._latched

    with pytest.raises(ExtractionPoolError):
        list(pool.stream(files))
    assert pool._latched
    assert pool._executor is None

    # Latched: still yields correct results, now in-process.
    got_latched = list(pool.stream(files))
    assert got_latched == [(pf, extract_file(pf)) for pf in files]


class _BreaksOnSubmitExecutor:
    """Unlike _AlwaysBreakingExecutor (which fails at .result()), this fails
    inside submit() itself -- the shape a real ProcessPoolExecutor takes once a
    worker has already died before the NEXT batch is even dispatched."""

    def submit(self, fn: Any, *args: Any) -> Any:
        raise extract_pool.BrokenProcessPool("worker already dead at submit time")

    def shutdown(self, wait: bool = True) -> None:
        pass


@pytest.mark.unit
def test_broken_process_pool_raised_from_submit_is_handled() -> None:
    """Regression: a real ProcessPoolExecutor can raise BrokenProcessPool from
    submit() itself, not only from a future's result() -- verified against a
    real ProcessPoolExecutor in review. The supervisor must still record the
    break (rebuild-or-latch) rather than let the raw exception escape
    unclassified and leave (_rebuilds, _generation, _latched) stale."""
    files = _python_files(3, size=50)
    pool = ExtractionPool(n_processes=0, batch_bytes=10)
    pool._n_processes = 2
    pool._executor = _BreaksOnSubmitExecutor()  # type: ignore[assignment]

    with pytest.raises(ExtractionPoolError) as excinfo:
        list(pool.stream(files))

    assert isinstance(excinfo.value.__cause__, extract_pool.BrokenProcessPool)
    assert pool._rebuilds == 1
    assert pool._generation == 1
    assert not pool._latched


# --- T6a/T6b: preflight probe -------------------------------------------------


@pytest.mark.unit
def test_preflight_fails_fast_when_ipc_queue_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """T6a: the sem_open/OSError half of "probe child exits nonzero (or
    ctx.Queue() raises OSError)". Fails before any child is even started."""

    class _BrokenContext:
        def Queue(self) -> Any:
            raise OSError("sem_open: No space left on device")

    monkeypatch.setattr(extract_pool.multiprocessing, "get_context", lambda name: _BrokenContext())

    with caplog.at_level(logging.WARNING, logger="indexer.extract_pool"):
        ok = extract_pool._run_preflight_probe()

    assert ok is False
    assert "could not create an IPC queue" in caplog.text


@pytest.mark.unit
def test_preflight_fails_fast_when_probe_child_crashes(caplog: pytest.LogCaptureFixture) -> None:
    """T6a: a real spawned child that exits nonzero is detected -- and the WARNING
    names the cause."""
    with caplog.at_level(logging.WARNING, logger="indexer.extract_pool"):
        ok = extract_pool._run_preflight_probe(target=_crashing_probe_target, timeout=10.0)

    assert ok is False
    assert "exited with code" in caplog.text


@pytest.mark.unit
def test_preflight_hang_is_killed_and_latches(caplog: pytest.LogCaptureFixture) -> None:
    """T6b: THE defect this plan was blocked on. A hung probe child must be
    killed within the injected timeout, never awaited to completion, and the
    test process itself must not hang."""
    start = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="indexer.extract_pool"):
        ok = extract_pool._run_preflight_probe(target=_hanging_probe_target, timeout=0.5)
    elapsed = time.monotonic() - start

    assert ok is False
    assert elapsed < 30.0  # nowhere near the child's 3600s sleep
    assert "did not respond within" in caplog.text
    assert "killed" in caplog.text


@pytest.mark.unit
def test_preflight_failure_latches_build_extraction_pool_to_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D8: build_extraction_pool degrades to a correct in-process pool, and no
    ProcessPoolExecutor is ever constructed, when the preflight probe fails."""
    monkeypatch.setattr(extract_pool, "_run_preflight_probe", lambda: False)

    pool = build_extraction_pool(_config(extract_processes=4))

    assert pool._executor is None
    files = _python_files(3)
    assert list(pool.stream(files)) == [(pf, extract_file(pf)) for pf in files]


# --- T7: sizing derivation ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("200000 100000\n", 2),
        ("100000 100000\n", 1),
        ("max 100000\n", None),
        ("bogus 100000\n", None),
        ("200000 0\n", None),
        ("200000\n", None),
        ("\n", None),
    ],
)
def test_cgroup_cpu_quota_parsing(
    monkeypatch: pytest.MonkeyPatch, content: str, expected: int | None
) -> None:
    monkeypatch.setattr(extract_pool.Path, "read_text", lambda self: content)
    assert extract_pool._cgroup_cpu_quota() == expected


@pytest.mark.unit
def test_cgroup_cpu_quota_unreadable_file_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(self: Any) -> str:
        raise OSError("no such file")

    monkeypatch.setattr(extract_pool.Path, "read_text", _raise)
    assert extract_pool._cgroup_cpu_quota() is None


@pytest.mark.unit
def test_available_cpus_prefers_affinity_over_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_pool.os, "sched_getaffinity", lambda pid: {0, 1, 2}, raising=False)
    monkeypatch.setattr(extract_pool.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(extract_pool, "_cgroup_cpu_quota", lambda: None)
    assert extract_pool._available_cpus() == 3


@pytest.mark.unit
def test_available_cpus_falls_back_to_cpu_count_without_affinity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(extract_pool.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(extract_pool.os, "cpu_count", lambda: 5)
    monkeypatch.setattr(extract_pool, "_cgroup_cpu_quota", lambda: None)
    assert extract_pool._available_cpus() == 5


@pytest.mark.unit
def test_available_cpus_clamped_by_cgroup_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        extract_pool.os, "sched_getaffinity", lambda pid: set(range(16)), raising=False
    )
    monkeypatch.setattr(extract_pool, "_cgroup_cpu_quota", lambda: 2)
    assert extract_pool._available_cpus() == 2


@pytest.mark.unit
def test_available_cpus_floors_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_pool.os, "sched_getaffinity", lambda pid: set(), raising=False)
    monkeypatch.setattr(extract_pool, "_cgroup_cpu_quota", lambda: None)
    assert extract_pool._available_cpus() == 1


@pytest.mark.unit
def test_derive_process_count_explicit_value_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_pool, "_available_cpus", lambda: 1)  # would derive to 1 if used
    assert derive_process_count(_config(extract_processes=6)) == 6


@pytest.mark.unit
def test_derive_process_count_kill_switch_is_explicit_one() -> None:
    assert derive_process_count(_config(extract_processes=1)) == 1


@pytest.mark.unit
def test_derive_process_count_derives_and_clamps_to_eight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_pool, "_available_cpus", lambda: 64)
    assert derive_process_count(_config()) == 8


@pytest.mark.unit
def test_derive_process_count_derives_below_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extract_pool, "_available_cpus", lambda: 3)
    assert derive_process_count(_config()) == 3


# --- T8: extract_processes: 1 creates no pool ----------------------------------


@pytest.mark.unit
def test_extract_processes_one_creates_no_pool() -> None:
    """T8: the kill switch. No subprocess is ever created (no executor object at
    all), and results are identical to calling extract_file directly."""
    pool = build_extraction_pool(_config(extract_processes=1))
    assert pool._executor is None

    files = _python_files(3)
    assert list(pool.stream(files)) == [(pf, extract_file(pf)) for pf in files]
