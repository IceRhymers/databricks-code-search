"""Measure embedding dispatch scaling under concurrency (#107, AC3 part 1).

Offline companion to ``app.embed.databricks_embedder``. Drives the SAME dispatch
code path AC3 cares about (batches -> ``ThreadPoolExecutor.map`` -> flatten)
against a fake client whose ``do()`` sleeps a fixed per-batch latency -- no
workspace, no network, not run in CI. It measures that the dispatch actually
parallelizes; it says nothing about the real AI Gateway's throughput or 429
behavior -- that is the other two halves of AC3's measurement protocol (a
real-repo run's ``embed=`` timing-line comparison, and a single named
``databricks.sdk.retries`` logger), which require a live workspace and are run
by hand, not by this script. See ``docs/runbooks/semantic-enablement.md`` §4.

Usage: ``uv run python scripts/measure_embedding_concurrency.py
[--latency-s 0.05] [--num-batches 32] [--concurrencies 1,2,4,8]``
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Sequence
from typing import Any

from app.embed import databricks_embedder


class _SleepingApiClient:
    """Stands in for ``WorkspaceClient.api_client``: sleeps ``latency_s`` per
    batch, then returns one dim-1 vector per text. Order/count correctness is
    already pinned by ``tests/unit/test_embed.py``; this script only measures
    wall clock."""

    def __init__(self, latency_s: float) -> None:
        self._latency_s = latency_s

    def do(self, method: str, path: str, *, body: dict[str, Any]) -> dict[str, Any]:
        time.sleep(self._latency_s)
        return {"data": [{"embedding": [0.0]} for _ in body["input"]]}


class _SleepingClient:
    def __init__(self, latency_s: float) -> None:
        self.api_client = _SleepingApiClient(latency_s)


def measure(*, num_batches: int, batch_size: int, latency_s: float, concurrency: int) -> float:
    """Run one ``embed()`` call over a synthetic corpus of ``num_batches``
    batches and return the wall-clock seconds."""
    texts = [f"text-{i}" for i in range(num_batches * batch_size)]
    client = _SleepingClient(latency_s)
    embed = databricks_embedder(
        "ep", "m", client=client, dim=1, batch_size=batch_size, concurrency=concurrency
    )
    start = time.perf_counter()
    vectors = embed(texts)
    elapsed = time.perf_counter() - start
    assert len(vectors) == len(texts)
    return elapsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latency-s", type=float, default=0.05, help="fake per-batch latency")
    parser.add_argument("--num-batches", type=int, default=32, help="synthetic batch count")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="texts per batch (kept small so --num-batches controls the batch count directly)",
    )
    parser.add_argument(
        "--concurrencies", default="1,2,4,8", help="comma-separated concurrency levels"
    )
    args = parser.parse_args(argv)
    concurrencies = [int(c) for c in args.concurrencies.split(",")]

    print(f"{args.num_batches} batches x {args.latency_s}s fake latency each")
    print(f"{'concurrency':>11s}  {'wall_clock_s':>12s}  {'speedup':>8s}")
    baseline: float | None = None
    for concurrency in concurrencies:
        elapsed = measure(
            num_batches=args.num_batches,
            batch_size=args.batch_size,
            latency_s=args.latency_s,
            concurrency=concurrency,
        )
        if baseline is None:
            baseline = elapsed
        speedup = baseline / elapsed if elapsed else float("inf")
        print(f"{concurrency:>11d}  {elapsed:>12.3f}  {speedup:>7.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
