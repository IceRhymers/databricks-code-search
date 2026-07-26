"""Measure symbol/edge extraction pool scaling against a REAL tarball (#108, AC1).

Offline companion to :mod:`indexer.extract_pool`. Drives the exact production call
site :mod:`indexer.job` uses: ``indexer.ingest.iter_tar_source_files(tarball)`` ->
``ExtractionPool.stream(...)`` -- never a batch-submit harness (``executor.map``
over a pre-built batch list) and never an on-disk walk. Since #106 the production
file source is a single serial gzip-decompress + decode + filter pass over one
open ``TarFile``, run once here and shared between the serial and pooled arms, so
this script also reports the ingest/extract split the pool cannot touch (see
``docs/runbooks/indexing-parallelism.md`` §3's "ingestion stays serial" note).

**The tarball must be real** -- a GitHub codeload archive (``curl -L
https://codeload.github.com/OWNER/REPO/tar.gz/HEAD -o repo.tar.gz`` or
``indexer.fetch.download_tarball`` by hand), not a synthetic fixture. A
site-packages-heavy Python tree (e.g. this repo's own ``.venv/``, tarred up) is a
reasonable large-repo proxy: ``tar czf repo.tar.gz --transform 's,^,repo-abc1234/,' .``

Usage: ``uv run python scripts/measure_extraction_pool.py --tarball repo.tar.gz
[--processes 2,4,8] [--batch-bytes 2000000] [--repeat 1]``
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from pathlib import Path

from indexer.extract_pool import _BATCH_BYTES, ExtractionPool
from indexer.ingest import iter_tar_source_files
from indexer.languages import FileExtraction, ParsedFile
from indexer.symbols import extract_file


def _load_files(tarball: Path) -> list[ParsedFile]:
    """One real, serial pass over the tarball -- the Amdahl floor (§4.7.1) this
    pool cannot parallelize. Timed separately by the caller."""
    return list(iter_tar_source_files(tarball))


def _measure_serial(
    files: list[ParsedFile],
) -> tuple[float, list[tuple[ParsedFile, FileExtraction]]]:
    start = time.perf_counter()
    results = [(pf, extract_file(pf)) for pf in files]
    return time.perf_counter() - start, results


def _measure_pooled(
    files: list[ParsedFile], *, n_processes: int, batch_bytes: int
) -> tuple[float, list[tuple[ParsedFile, FileExtraction]]]:
    pool = ExtractionPool(n_processes=n_processes, batch_bytes=batch_bytes)
    try:
        start = time.perf_counter()
        results = list(pool.stream(files))
        elapsed = time.perf_counter() - start
    finally:
        pool.shutdown()
    return elapsed, results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tarball", required=True, type=Path, help="a real downloaded repo tarball (.tar.gz)"
    )
    parser.add_argument(
        "--processes", default="2,4,8", help="comma-separated worker-process counts to measure"
    )
    parser.add_argument("--batch-bytes", type=int, default=_BATCH_BYTES)
    parser.add_argument("--repeat", type=int, default=1, help="repetitions per data point")
    args = parser.parse_args(argv)
    process_counts = [int(p) for p in args.processes.split(",")]

    print(f"loading {args.tarball} ...")
    ingest_start = time.perf_counter()
    files = _load_files(args.tarball)
    ingest_elapsed = time.perf_counter() - ingest_start
    qualifying = [pf for pf in files if pf.lang is not None]
    total_bytes = sum(len(pf.content) for pf in files)
    qualifying_bytes = sum(len(pf.content) for pf in qualifying)
    print(
        f"{len(files)} indexable files / {total_bytes / 1e6:.1f} MB, "
        f"{len(qualifying)} qualify for parsing / {qualifying_bytes / 1e6:.1f} MB "
        f"(ingest: {ingest_elapsed:.2f}s -- serial in the parent, both arms below)"
    )

    print(f"\n{'path':>14s}  {'extract_s':>10s}  {'total_s':>10s}  {'speedup':>8s}  {'note':>9s}")
    serial_extract = None
    serial_results: list[tuple[ParsedFile, FileExtraction]] = []
    for _ in range(args.repeat):
        elapsed, serial_results = _measure_serial(files)
        if serial_extract is None or elapsed < serial_extract:
            serial_extract = elapsed
    assert serial_extract is not None
    serial_total = ingest_elapsed + serial_extract
    print(f"{'serial':>14s}  {serial_extract:>10.3f}  {serial_total:>10.3f}  {1.0:>7.2f}x")

    for n in process_counts:
        best_elapsed = None
        identical = True
        for _ in range(args.repeat):
            elapsed, pooled_results = _measure_pooled(
                files, n_processes=n, batch_bytes=args.batch_bytes
            )
            identical = identical and pooled_results == serial_results
            if best_elapsed is None or elapsed < best_elapsed:
                best_elapsed = elapsed
        assert best_elapsed is not None
        total = ingest_elapsed + best_elapsed
        speedup = serial_total / total if total else float("inf")
        note = "identical" if identical else "MISMATCH"
        print(
            f"{f'pool x{n}':>14s}  {best_elapsed:>10.3f}  {total:>10.3f}  "
            f"{speedup:>7.2f}x  {note:>9s}"
        )

    print(
        f"\ningest (serial, shared by both arms): {ingest_elapsed:.2f}s of "
        f"{serial_total:.2f}s serial total ({100 * ingest_elapsed / serial_total:.0f}%) -- "
        "the floor AC1's 'combined' speedup above cannot cross, however many "
        "processes extract_processes uses."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
