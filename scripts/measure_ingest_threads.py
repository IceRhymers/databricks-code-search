"""Measure `iter_tar_source_files` thread-scaling and its GIL-bound component
mix (#109 §3.4, E4 -- "M2: does the ingest pass scale across threads?").

Since #106 the per-branch file source is a single serial pass over one open
``TarFile`` (:func:`indexer.ingest.iter_tar_source_files`): a ``tf.next()``
member walk, ``fh.read()``, the NUL-sniff binary check
(:func:`indexer.parse._looks_binary`), a UTF-8 decode, and a NUL-strip. Raising
``index_concurrency`` multiplies *concurrent* ingest passes across repo-worker
threads, and whether that helps is gated on which of those steps hold the GIL.
A planning-time synthetic probe (40 MB payload, isolated ``bytes.decode`` vs
``zlib.decompress``) found decode GIL-bound and zlib GIL-releasing; this script
re-measures the FULL real pass, decomposed, over real tarballs, rather than
re-quoting that probe.

Two things are measured, and the "decompose by component" and "end-to-end
speedup" numbers deliberately come from two different code paths per the
plan's own instruction:

* **Component breakdown** -- a re-walk of the tarball using the SAME private
  primitives ``iter_tar_source_files`` calls (``indexer.ingest
  ._normalise_member_name`` / ``._assert_link_target_is_contained``,
  ``indexer.parse._looks_binary``), imported rather than re-forked, with a
  ``time.perf_counter()`` pair around each step. This is a timing-only
  reimplementation of the loop -- never the source of the speedup numbers.
* **End-to-end speedup** -- always calls the REAL
  ``indexer.ingest.iter_tar_source_files`` and nothing else, N threads each
  streaming its OWN distinct real tarball (page cache and content mix must not
  be shared across threads), compared against a single-thread sequential pass
  over the SAME N tarballs.

Two conditions: CPU otherwise idle, and with :class:`indexer.extract_pool.
ExtractionPool` constructed and continuously driving real ``.stream()``
extraction on a background thread, to see whether process-pool contention for
cores changes the picture.

**The tarballs must be real** -- fetched over HTTP from public GitHub repos via
:func:`indexer.fetch.download_tarball` (unauthenticated ``httpx.Client`` works
fine for public repos) and cached locally so a re-run doesn't re-download.

Usage: ``uv run python scripts/measure_ingest_threads.py [--repeat 3]
[--pool-processes 4] [--cache-dir /tmp/measure_ingest_threads_cache]
[--repos org/repo@ref,org/repo@ref,...]`` (need >= 4 distinct repos).
"""

from __future__ import annotations

import argparse
import gzip
import tarfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx

from indexer.extract_pool import ExtractionPool
from indexer.fetch import download_tarball
from indexer.ingest import (
    MAX_EXTRACTED_BYTES,
    _assert_link_target_is_contained,
    _normalise_member_name,
    iter_tar_source_files,
)
from indexer.languages import MAX_FILE_BYTES
from indexer.parse import _looks_binary

T = TypeVar("T")

# Four distinct public repos of comparable decoded size (~20-37 MB each,
# verified by hand before picking these four) -- comparable size matters for
# the N=4 comparison specifically: four wildly mismatched tarballs would let
# the largest one dominate both the sequential sum and the concurrent wall
# clock, measuring "how fast is the biggest repo alone" rather than genuine
# 4-way thread scaling.
DEFAULT_REPOS = [
    ("tiangolo", "fastapi", "master"),
    ("sqlalchemy", "sqlalchemy", "main"),
    ("sympy", "sympy", "master"),
    ("django", "django", "main"),
]


def _parse_repos(spec: str) -> list[tuple[str, str, str]]:
    out = []
    for item in spec.split(","):
        org_repo, _, ref = item.partition("@")
        org, _, repo = org_repo.partition("/")
        out.append((org, repo, ref or "HEAD"))
    return out


def _cache_path(cache_dir: Path, org: str, repo: str) -> Path:
    return cache_dir / f"{org}__{repo}.tar.gz"


def fetch_tarballs(repos: Sequence[tuple[str, str, str]], cache_dir: Path) -> list[Path]:
    """Download each repo's tarball once (cached under ``cache_dir`` across runs)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with httpx.Client(timeout=120.0) as client:
        for org, repo, ref in repos:
            dest = _cache_path(cache_dir, org, repo)
            if not dest.exists():
                print(f"fetching {org}/{repo}@{ref} ...")
                tmp_dir = cache_dir / f"_dl_{org}_{repo}"
                downloaded = download_tarball(client, org, repo, ref, tmp_dir)
                downloaded.replace(dest)
                try:
                    tmp_dir.rmdir()
                except OSError:
                    pass
            paths.append(dest)
    return paths


@dataclass
class ComponentTimes:
    """Cumulative wall-clock seconds per component of one (or more, summed)
    instrumented walks -- see :func:`decompose_walk`."""

    tf_next: float = 0.0
    fh_read: float = 0.0
    looks_binary: float = 0.0
    decode: float = 0.0
    nul_strip: float = 0.0
    n_files: int = 0
    n_members: int = 0

    def add(self, other: "ComponentTimes") -> None:
        self.tf_next += other.tf_next
        self.fh_read += other.fh_read
        self.looks_binary += other.looks_binary
        self.decode += other.decode
        self.nul_strip += other.nul_strip
        self.n_files += other.n_files
        self.n_members += other.n_members

    @property
    def total(self) -> float:
        return self.tf_next + self.fh_read + self.looks_binary + self.decode + self.nul_strip


def decompose_walk(tar_path: Path) -> ComponentTimes:
    """Re-walk ``tar_path`` with the exact filter chain
    ``indexer.ingest.iter_tar_source_files`` uses, timing each component with
    its own ``perf_counter()`` pair.

    A TIMING-ONLY reimplementation of that function's loop body: it imports the
    same private primitives (``_normalise_member_name``,
    ``_assert_link_target_is_contained``, ``_looks_binary``) rather than
    re-forking their logic, so the filter population -- which members get as
    far as ``fh.read()`` / decode -- matches production exactly. It does not
    build ``ParsedFile`` objects or return content; the real end-to-end number
    always comes from calling ``iter_tar_source_files`` itself (see
    :func:`run_sequential_real` / :func:`run_concurrent_real`), never from this
    function.
    """
    ct = ComponentTimes()
    tf = tarfile.open(tar_path, mode="r:gz")
    try:
        top_dir: str | None = None
        streamed = 0
        seen: set[str] = set()
        while True:
            t0 = time.perf_counter()
            member = tf.next()
            ct.tf_next += time.perf_counter() - t0
            if member is None:
                break
            ct.n_members += 1
            tf.members.clear()  # type: ignore[attr-defined]

            name = _normalise_member_name(member.name)
            component = name.split("/", 1)[0]
            if top_dir is None:
                top_dir = component
            elif component != top_dir:
                raise ValueError(
                    "expected exactly one top-level dir in tarball, found "
                    f"{sorted({top_dir, component})}"
                )

            if member.islnk() or member.issym():
                _assert_link_target_is_contained(name, member.linkname)

            if member.offset > MAX_EXTRACTED_BYTES:
                raise ValueError(
                    f"tarball stream reaches {member.offset} decompressed bytes, "
                    f"exceeding {MAX_EXTRACTED_BYTES}"
                )

            if not member.isreg():
                continue

            streamed += member.size
            if streamed > MAX_EXTRACTED_BYTES:
                raise ValueError(
                    f"tarball streams to {streamed} bytes of content, "
                    f"exceeding {MAX_EXTRACTED_BYTES}"
                )

            rel_path = name[len(top_dir) + 1 :] if name != top_dir else ""
            if not rel_path:
                continue
            if ".git" in rel_path.split("/"):
                continue
            if member.size > MAX_FILE_BYTES:
                continue
            if rel_path in seen:
                continue
            seen.add(rel_path)

            fh = tf.extractfile(member)
            if fh is None:
                continue
            t0 = time.perf_counter()
            with fh:
                raw = fh.read()
            ct.fh_read += time.perf_counter() - t0

            t0 = time.perf_counter()
            is_binary = _looks_binary(raw)
            ct.looks_binary += time.perf_counter() - t0
            if is_binary:
                continue

            t0 = time.perf_counter()
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                ct.decode += time.perf_counter() - t0
                continue
            ct.decode += time.perf_counter() - t0

            t0 = time.perf_counter()
            content.replace("\x00", "")
            ct.nul_strip += time.perf_counter() - t0
            ct.n_files += 1
    finally:
        tf.close()
    return ct


def measure_zlib_isolated(tar_path: Path) -> float:
    """Whole-archive gzip inflate, isolated from tarfile header parsing --
    the cleanest available measurement of the GIL-releasing component named in
    the plan's §0.4 prior (``zlib.decompress``, 2.54x at 4 threads on a 40 MB
    synthetic payload)."""
    raw_gz = tar_path.read_bytes()
    start = time.perf_counter()
    gzip.decompress(raw_gz)
    return time.perf_counter() - start


def best_of(fn: Callable[[], T], repeat: int, key: Callable[[T], float]) -> T:
    """>=3 repeats, discard one warm-up, report best-of -- the methodology
    ``docs/perf/issue-108-measurements.md`` used for #108."""
    results = [fn() for _ in range(max(repeat, 1))]
    kept = results[1:] if len(results) > 1 else results
    return min(kept, key=key)


def run_sequential_real(paths: Sequence[Path]) -> float:
    """Single thread, ``iter_tar_source_files`` over every path in ``paths``,
    one after another -- the baseline half of the fair sequential-vs-concurrent
    comparison (same tarball set both sides)."""
    start = time.perf_counter()
    for p in paths:
        list(iter_tar_source_files(p))
    return time.perf_counter() - start


def run_concurrent_real(paths: Sequence[Path]) -> float:
    """``len(paths)`` threads, each streaming its OWN tarball via the REAL
    ``iter_tar_source_files`` concurrently."""
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(paths)) as ex:
        futures = [ex.submit(lambda p=p: list(iter_tar_source_files(p))) for p in paths]
        for f in futures:
            f.result()
    return time.perf_counter() - start


def run_concurrent_decomp(paths: Sequence[Path]) -> tuple[float, ComponentTimes]:
    """``len(paths)`` threads, each running the instrumented
    :func:`decompose_walk` on its own tarball concurrently. Returns the
    wall-clock for the whole concurrent run plus the SUM of every thread's
    per-component cumulative time, so "does component X's aggregate cost grow
    linearly with N" is directly readable off two consecutive rows."""
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(paths)) as ex:
        results = [f.result() for f in [ex.submit(decompose_walk, p) for p in paths]]
    elapsed = time.perf_counter() - start
    total = ComponentTimes()
    for r in results:
        total.add(r)
    return elapsed, total


class PoolContention:
    """Drives ``ExtractionPool.stream()`` continuously on a background thread
    over a fixed file list, to create real multi-process CPU contention for
    the "pool live" condition. Started/stopped once per condition, spanning
    every N in that condition's matrix."""

    def __init__(self, files: list, n_processes: int) -> None:
        self._pool = ExtractionPool(n_processes=n_processes)
        self._files = files
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            list(self._pool.stream(self._files))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
        self._pool.shutdown()


def _bucket(n: int, speedup: float) -> str:
    """Fixed-in-advance thresholds from the plan's §3.4."""
    if n == 2:
        if speedup >= 1.6:
            return "PARALLELIZES"
        if speedup <= 1.1:
            return "NO-SCALE"
        return "ambiguous"
    if n == 4:
        if speedup >= 2.0:
            return "PARALLELIZES"
        if speedup <= 1.2:
            return "NO-SCALE"
        return "PARTIAL"
    return "n/a"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp/measure_ingest_threads_cache"))
    parser.add_argument("--repeat", type=int, default=3, help=">=3 per the plan's protocol")
    parser.add_argument(
        "--pool-processes",
        type=int,
        default=4,
        help="ExtractionPool size for the 'pool live' condition",
    )
    parser.add_argument(
        "--repos",
        default=",".join(f"{o}/{r}@{ref}" for o, r, ref in DEFAULT_REPOS),
        help="comma-separated org/repo@ref list, need >= 4 distinct repos",
    )
    args = parser.parse_args(argv)

    repos = _parse_repos(args.repos)
    if len(repos) < 4:
        raise SystemExit(
            "need >= 4 distinct repos for the N in {1..4} thread-scaling matrix, "
            f"got {len(repos)}"
        )

    paths = fetch_tarballs(repos, args.cache_dir)

    print("=" * 100)
    print(f"tarballs (cached under {args.cache_dir}):")
    for (org, repo, ref), p in zip(repos, paths, strict=True):
        files = list(iter_tar_source_files(p))
        total_bytes = sum(len(pf.content) for pf in files)
        print(
            f"  {org}/{repo}@{ref}: {p.name}, {p.stat().st_size / 1e6:.2f} MB compressed, "
            f"{len(files)} indexable files, {total_bytes / 1e6:.2f} MB decoded text"
        )
    print("=" * 100)

    # ---- Part 1: per-tarball component breakdown, single-threaded, idle CPU ----
    print("\n### Part 1 -- per-tarball component breakdown (single-threaded, CPU idle) ###")
    print("component times are cumulative seconds inside the instrumented re-walk (see docstring);")
    print(
        "zlib_iso_s is a SEPARATE standalone whole-archive gzip.decompress(), not part of the "
        "walk sum.\n"
    )
    print(
        f"{'repo':>22s}  {'zlib_iso_s':>10s}  {'tf_next_s':>10s}  {'fh_read_s':>10s}  "
        f"{'binary_s':>9s}  {'decode_s':>9s}  {'nulstrip_s':>10s}  {'n_files':>7s}  "
        f"{'n_members':>9s}"
    )
    for (org, repo, ref), p in zip(repos, paths, strict=True):
        zlib_s = best_of(lambda p=p: measure_zlib_isolated(p), args.repeat, key=lambda x: x)
        ct = best_of(lambda p=p: decompose_walk(p), args.repeat, key=lambda c: c.total)
        label = f"{org}/{repo}"
        print(
            f"{label:>22s}  {zlib_s:>10.4f}  {ct.tf_next:>10.4f}  {ct.fh_read:>10.4f}  "
            f"{ct.looks_binary:>9.4f}  {ct.decode:>9.4f}  {ct.nul_strip:>10.4f}  "
            f"{ct.n_files:>7d}  {ct.n_members:>9d}"
        )

    # ---- Part 2: N-thread scaling matrix, two conditions ----
    for condition in ("idle", "pool_live"):
        print(f"\n### Part 2 -- N-thread ingest scaling, condition={condition} ###")
        contention: PoolContention | None = None
        if condition == "pool_live":
            load_files = list(iter_tar_source_files(paths[0]))
            print(
                f"starting background ExtractionPool(n_processes={args.pool_processes}) "
                f"driving .stream() over {len(load_files)} files from "
                f"{repos[0][0]}/{repos[0][1]} ..."
            )
            contention = PoolContention(load_files, n_processes=args.pool_processes)
            contention.start()
            time.sleep(0.5)  # let the process pool spin up before timing starts
        try:
            print(
                "\nend-to-end speedup (REAL iter_tar_source_files; same N-tarball set both sides):"
            )
            print(
                f"{'N':>3s}  {'seq_s (1thr, Nx)':>17s}  {'conc_s (Nthr)':>14s}  "
                f"{'speedup':>9s}  {'bucket':>13s}"
            )
            for n in (1, 2, 3, 4):
                subset = paths[:n]
                seq = best_of(
                    lambda subset=subset: run_sequential_real(subset), args.repeat, key=lambda x: x
                )
                if n > 1:
                    conc = best_of(
                        lambda subset=subset: run_concurrent_real(subset),
                        args.repeat,
                        key=lambda x: x,
                    )
                else:
                    conc = seq
                speedup = seq / conc if conc else float("inf")
                bucket = _bucket(n, speedup)
                print(f"{n:>3d}  {seq:>17.4f}  {conc:>14.4f}  {speedup:>8.2f}x  {bucket:>13s}")

            print(
                "\ncomponent breakdown at each N (instrumented re-walk, N threads concurrent, "
                "SUM across threads):"
            )
            print(
                f"{'N':>3s}  {'wall_s':>8s}  {'sum_tf_next_s':>13s}  {'sum_fh_read_s':>13s}  "
                f"{'sum_binary_s':>12s}  {'sum_decode_s':>12s}  {'sum_nulstrip_s':>14s}"
            )
            for n in (1, 2, 3, 4):
                subset = paths[:n]
                elapsed, ct = best_of(
                    lambda subset=subset: run_concurrent_decomp(subset),
                    args.repeat,
                    key=lambda t: t[0],
                )
                print(
                    f"{n:>3d}  {elapsed:>8.4f}  {ct.tf_next:>13.4f}  {ct.fh_read:>13.4f}  "
                    f"{ct.looks_binary:>12.4f}  {ct.decode:>12.4f}  {ct.nul_strip:>14.4f}"
                )
        finally:
            if contention is not None:
                contention.stop()
                print("stopped background ExtractionPool contention.")

    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
