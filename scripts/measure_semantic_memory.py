"""Measure the semantic path's peak memory, decomposed into named terms (#109, AC3).

Offline companion to ``docs/perf/issue-109-measurements.md`` §3.3(b)'s ``P_worst``
model. Drives the REAL production call sequence from
``indexer.job._index_one_branch``'s semantic path -- never a re-implementation:

    files = list(iter_tar_source_files(tar_path))                       # alpha
    files_to_embed = [pf for pf in files                                # the delta gate
                       if (pf.path, content_sha(pf.content)) not in carried]
    chunk_writer = indexer.job._precompute_chunk_writer(                # gamma + vectors
        files_to_embed, embed_fn, max_chunks_per_repo)

``_precompute_chunk_writer`` is private (leading underscore) but imported directly
by name -- this is a measurement script living in the same repo, not an external
consumer, and the alternative (re-typing its chunking/embedding logic here) is
exactly the drift this script exists to avoid.

**The stub embedder returns DISTINCT floats per vector component**
(``float(i * dim + j)``), never a shared/cached float such as ``[0.0] * dim``.
Planning found that the naive stub understates resident memory by ~4x, because
``[0.0] * dim`` stores ``dim`` references to ONE cached float object rather than
``dim`` independent ``PyFloat`` allocations (see the plan's §2.2). This script's
correctness as a memory probe depends entirely on avoiding that trap.

**Methodology -- three named RSS terms per corpus/gate combination:**

Peak RSS (``resource.getrusage(RUSAGE_SELF).ru_maxrss``) is a monotonic
non-decreasing high-water mark *within one process*, so every stage below runs in
its OWN freshly spawned subprocess (this same script, re-invoked with a hidden
``--worker`` flag) -- otherwise stage N's baseline would already carry stage
N-1's peak forward and every delta after the first would be contaminated. Inside
one subprocess, three readings bound three terms:

  1. baseline                                                  (interpreter only)
  2. after ``files = list(iter_tar_source_files(tar))``        -> the FILES term (alpha)
  3. after ``per_file = {p: list(iter_chunks(p)) for p in files_to_embed}``
                                                                 -> the CHUNKS term (gamma)
  4. after ``_precompute_chunk_writer(files_to_embed, stub_embed_fn, huge_cap)``
                                                                 -> the VECTORS term

Between readings 3 and 4 the manually-built ``per_file`` dict from step 3 is
deleted and ``gc.collect()``ed *before* calling the real ``_precompute_chunk_writer``,
which re-chunks internally (it has no way to accept precomputed chunks -- that
would be re-implementing its contract, not measuring it). The intent is that the
freed step-3 allocations are reused by the allocator for step 4's structurally
identical rebuild, so the reading-3-to-4 delta is dominated by the NEW allocation
(the embedding vectors) rather than by double-counting the chunk objects. This is
a reasonable approximation on CPython/glibc for same-shaped allocations, not a
guarantee -- reported numbers may run slightly high for exactly this reason, and
that is called out again in the printed report.

The chunk cap passed to ``_precompute_chunk_writer`` here is deliberately huge
(never the production ``semantic_max_chunks_per_repo``): this script measures the
UNCAPPED terms (alpha, gamma) and the per-chunk vector cost directly, not
cap-breach behavior -- that is a different, later step of issue #109's plan
(§3.3(c)-(f)), not this script's job.

**V_cap (resident bytes per chunk)** is measured completely separately from the
corpus runs, matching the plan's §2.2 methodology exactly: build exactly 8000
distinct-float 1024-dim vectors directly (not through ``_precompute_chunk_writer``)
and take one baseline/after delta.

**N-concurrency (N in {1,2,3,4})** spins up N ``threading.Thread``s, each running
the SAME files -> chunk_writer pipeline against a real tarball, with ONE shared
``indexer.extract_pool.ExtractionPool`` built up front and each thread draining
its own ``pool.stream(files)`` call to completion -- so the #108 process pool's
own resident overhead (R_proc) is live and contributing to the measured peak,
exactly like N concurrent repo-worker threads in production. Each thread builds
its own ``files`` list from its own call to ``iter_tar_source_files`` (never a
shared generator -- the module's own docstring warns that a second thread
advancing the same tar-backed generator corrupts output silently). Both
``RUSAGE_SELF`` (this process, all threads) and ``RUSAGE_CHILDREN`` (the pool's
worker processes) are reported.

**Corpora**: this repo (``IceRhymers/databricks-code-search``) at HEAD, plus three
other modest real public repos spanning a size range (Flask, Requests, Django),
fetched via the real ``indexer.fetch.download_tarball`` and cached under
``--cache-dir`` so repeat runs do not re-download. Total download is a few tens of
MB, well under the ~200 MB budget.

Usage: ``uv run python scripts/measure_semantic_memory.py``
(no arguments needed for the default corpus set; see ``--help`` for overrides).
"""

from __future__ import annotations

import argparse
import gc
import itertools
import json
import logging
import random
import resource
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from indexer.extract_pool import ExtractionPool, _available_cpus
from indexer.fetch import download_tarball
from indexer.hashing import content_sha
from indexer.ingest import iter_tar_source_files
from indexer.languages import ParsedFile
from indexer.parse import iter_chunks

logging.disable(logging.CRITICAL)  # keep worker-subprocess stdout free of log noise

# A generous cap -- never the production semantic_max_chunks_per_repo -- so
# _precompute_chunk_writer never raises the cap-breach ValueError while this
# script measures the uncapped terms it deliberately does not exercise here.
_UNCAPPED_MAX_CHUNKS_PER_REPO = 50_000_000

# Default corpus: this repo plus three modest, well-known public repos spanning
# a size range. `ref` is always "HEAD" -- download_tarball's `ref` argument is
# passed straight into GitHub's tarball URL and accepts a branch name.
_DEFAULT_CORPORA = [
    ("databricks-code-search", "IceRhymers", "databricks-code-search"),
    ("flask", "pallets", "flask"),
    ("requests", "psf", "requests"),
    ("django", "django", "django"),
]

_GATE_STATES: list[tuple[str, float]] = [
    ("first-index", 0.0),  # gate closed: files_to_embed IS files, no narrowing
    ("recurring-1pct", 0.01),  # gate open: carried covers ~99% of files
    ("recurring-10pct", 0.10),  # gate open: carried covers ~90% of files
]


def _rss_kb() -> int:
    """Peak RSS of THIS process so far, in KB (Linux ``ru_maxrss`` semantics)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _children_rss_kb() -> int:
    """Peak RSS across reaped child processes, in KB."""
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss


def _stub_embed_fn(dim: int = 1024):
    """A stub ``EmbedFn`` returning DISTINCT floats per vector component.

    NEVER ``[0.0] * dim`` -- that stores ``dim`` references to one cached float
    and understates resident memory by ~4x (planning's §2.2 trap). A running
    counter guarantees every float, across every call, is a fresh ``PyFloat``.
    """
    counter = itertools.count()

    def embed_fn(texts: list[str]) -> list[list[float]]:
        vectors = []
        for _ in texts:
            base = next(counter) * dim
            vectors.append([float(base + j) for j in range(dim)])
        return vectors

    return embed_fn


def _source_bytes(files: Sequence[ParsedFile]) -> int:
    """Total UTF-8-encoded byte length of ``files``' content -- the denominator
    for both alpha and gamma."""
    return sum(len(pf.content.encode("utf-8")) for pf in files)


def _narrow_files_to_embed(
    files: list[ParsedFile], gate: str, delta_fraction: float, *, seed: int = 0
) -> list[ParsedFile]:
    """Replicate ``_index_one_branch``'s exact narrowing logic for a simulated
    gate state.

    ``gate == "first-index"``: the gate is CLOSED (no stamp at the current
    ``INDEX_SEMANTICS_VERSION``), so production sets ``files_to_embed = files``
    verbatim -- no ``carried`` set is even read. This is the shape that OOMs
    (§0.2 of the plan): no narrowing benefit at all.

    Otherwise: the gate is OPEN, and ``carried`` is simulated as covering
    ``1 - delta_fraction`` of ``files``' ``(path, content_sha)`` pairs (a
    deterministic random sample), so ``files_to_embed`` narrows to approximately
    ``delta_fraction`` of ``files`` -- using the SAME list-comprehension shape
    ``indexer/job.py`` uses, not a re-derived equivalent.
    """
    if gate == "first-index":
        return files
    shas = [(pf.path, content_sha(pf.content)) for pf in files]
    n_carry = round(len(shas) * (1 - delta_fraction))
    rng = random.Random(seed)
    carried = set(rng.sample(shas, n_carry)) if shas else set()
    return [pf for pf in files if (pf.path, content_sha(pf.content)) not in carried]


# --------------------------------------------------------------------------
# Worker bodies -- each runs in ITS OWN freshly spawned subprocess (see the
# module docstring for why ru_maxrss's high-water-mark semantics demand this).
# --------------------------------------------------------------------------


def _worker_stage(cfg: dict[str, Any]) -> dict[str, Any]:
    from indexer.job import _precompute_chunk_writer  # local: keep worker startup lean

    tarball = Path(cfg["tarball"])
    gate = cfg["gate"]
    delta_fraction = cfg["delta_fraction"]

    baseline_kb = _rss_kb()

    files = list(iter_tar_source_files(tarball))
    after_files_kb = _rss_kb()

    files_to_embed = _narrow_files_to_embed(files, gate, delta_fraction)

    per_file_chunks = {pf.path: list(iter_chunks(pf)) for pf in files_to_embed}
    after_chunks_kb = _rss_kb()
    chunk_count = sum(len(chunks) for chunks in per_file_chunks.values())
    chunk_content_bytes = sum(
        len(c.content.encode("utf-8")) for chunks in per_file_chunks.values() for c in chunks
    )
    del per_file_chunks
    gc.collect()

    embed_fn = _stub_embed_fn()
    chunk_writer = _precompute_chunk_writer(files_to_embed, embed_fn, _UNCAPPED_MAX_CHUNKS_PER_REPO)
    after_vectors_kb = _rss_kb()
    del chunk_writer

    return {
        "baseline_kb": baseline_kb,
        "after_files_kb": after_files_kb,
        "after_chunks_kb": after_chunks_kb,
        "after_vectors_kb": after_vectors_kb,
        "files_count": len(files),
        "files_bytes": _source_bytes(files),
        "files_to_embed_count": len(files_to_embed),
        "files_to_embed_bytes": _source_bytes(files_to_embed),
        "chunk_count": chunk_count,
        "chunk_content_bytes": chunk_content_bytes,
    }


def _worker_vcap(cfg: dict[str, Any]) -> dict[str, Any]:
    n = cfg["n"]
    dim = cfg["dim"]
    baseline_kb = _rss_kb()
    vectors = [[float(i * dim + j) for j in range(dim)] for i in range(n)]
    after_kb = _rss_kb()
    assert len(vectors) == n and len(vectors[0]) == dim
    return {"baseline_kb": baseline_kb, "after_kb": after_kb, "n": n, "dim": dim}


def _worker_nconc(cfg: dict[str, Any]) -> dict[str, Any]:
    from indexer.job import _precompute_chunk_writer  # local: keep worker startup lean

    tarball = Path(cfg["tarball"])
    n_threads = cfg["n_threads"]
    gate = cfg["gate"]
    delta_fraction = cfg["delta_fraction"]

    baseline_self_kb = _rss_kb()
    baseline_children_kb = _children_rss_kb()

    n_processes = min(_available_cpus(), 8)
    pool = ExtractionPool(n_processes=n_processes)

    errors: list[str] = []

    def run_one() -> None:
        try:
            files = list(iter_tar_source_files(tarball))
            files_to_embed = _narrow_files_to_embed(files, gate, delta_fraction)
            embed_fn = _stub_embed_fn()
            # Held alive (not discarded) across pool.stream() below: production
            # (indexer/job.py) keeps chunk_writer's vectors resident for the whole
            # index_repo write window, which is exactly the concurrent-residency
            # property this stage measures -- freeing it early would let each
            # thread's vectors be collected before, or concurrently with, sibling
            # threads' peaks, understating true N-way concurrent RSS.
            chunk_writer = _precompute_chunk_writer(
                files_to_embed, embed_fn, _UNCAPPED_MAX_CHUNKS_PER_REPO
            )
            # Drain the pool's stream fully so its worker processes actually do
            # (and stay resident for) the same work a real branch would ask of them.
            list(pool.stream(files))
            del chunk_writer
        except Exception as exc:  # noqa: BLE001 -- reported, not swallowed
            errors.append(repr(exc))

    threads = [threading.Thread(target=run_one) for _ in range(n_threads)]
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    pool.shutdown()

    after_self_kb = _rss_kb()
    after_children_kb = _children_rss_kb()

    return {
        "n_threads": n_threads,
        "n_processes": n_processes,
        "baseline_self_kb": baseline_self_kb,
        "after_self_kb": after_self_kb,
        "baseline_children_kb": baseline_children_kb,
        "after_children_kb": after_children_kb,
        "elapsed_s": elapsed,
        "errors": errors,
    }


_WORKERS = {"stage": _worker_stage, "vcap": _worker_vcap, "nconc": _worker_nconc}


def _run_worker_subprocess(mode: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Re-invoke THIS script in a fresh interpreter to run one measurement.

    Fresh process per measurement is load-bearing, not a style choice: ``ru_maxrss``
    only grows within a process, so reusing one process across stages/corpora would
    let an earlier, larger measurement's peak silently leak into a later, smaller
    one's baseline.
    """
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", mode, json.dumps(cfg)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _emit(**fields: object) -> None:
    print(" ".join(f"{k}={v}" for k, v in fields.items()))


# --------------------------------------------------------------------------
# Orchestration (normal, non-worker invocation)
# --------------------------------------------------------------------------


def _download_corpora(corpora: list[tuple[str, str, str]], cache_dir: Path) -> dict[str, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(timeout=120.0)
    paths: dict[str, Path] = {}
    for name, org, repo in corpora:
        dest = cache_dir / name
        tar_path = dest / "source.tar.gz"
        if tar_path.exists():
            print(f"# {name}: using cached {tar_path} ({tar_path.stat().st_size} bytes)")
        else:
            print(f"# {name}: downloading {org}/{repo}@HEAD ...")
            download_tarball(client, org, repo, "HEAD", dest)
            print(f"# {name}: downloaded {tar_path} ({tar_path.stat().st_size} bytes)")
        paths[name] = tar_path
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Hidden worker-dispatch path: `--worker <mode> <json-config>`. Not a public
    # CLI surface -- it exists purely so `_run_worker_subprocess` can re-invoke
    # this file in a fresh interpreter for one isolated measurement.
    if argv and argv[0] == "--worker":
        mode, raw_cfg = argv[1], argv[2]
        result = _WORKERS[mode](json.loads(raw_cfg))
        print(json.dumps(result))
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("/tmp/measure_semantic_memory_cache"),
        help="where downloaded tarballs are cached across runs",
    )
    parser.add_argument(
        "--corpora",
        default=",".join(f"{org}/{repo}" for _, org, repo in _DEFAULT_CORPORA),
        help=(
            "comma-separated org/repo list, in size order (last is used for the N-concurrency arm)"
        ),
    )
    parser.add_argument("--dim", type=int, default=1024, help="embedding dimension")
    parser.add_argument("--vcap-n", type=int, default=8000, help="vector count for the V_cap probe")
    parser.add_argument(
        "--n-threads", default="1,2,3,4", help="comma-separated N values for the concurrency arm"
    )
    parser.add_argument(
        "--nconc-corpus",
        default=None,
        help="corpus name to use for the N-concurrency arm (default: the last/largest corpus)",
    )
    args = parser.parse_args(argv)

    corpus_specs = []
    for entry in args.corpora.split(","):
        org, repo = entry.split("/", 1)
        corpus_specs.append((repo, org, repo))
    # Prefer the default's friendly names when they line up (cosmetic only).
    default_by_org_repo = {(org, repo): name for name, org, repo in _DEFAULT_CORPORA}
    corpus_specs = [
        (default_by_org_repo.get((org, repo), repo), org, repo) for _, org, repo in corpus_specs
    ]

    print("=" * 78)
    print("environment")
    print("=" * 78)
    _emit(python=sys.version.split()[0], cpu_count_affinity=_available_cpus())
    try:
        import shutil as _shutil

        total, _used, free = _shutil.disk_usage("/tmp")
        _emit(tmp_total_bytes=total, tmp_free_bytes=free, note="a-tmpfs-box-per-plan-1.1")
    except OSError:
        pass
    print()

    tarballs = _download_corpora(corpus_specs, args.cache_dir)
    print()

    print("=" * 78)
    print("stage 1: alpha (files) / gamma (chunks) / vectors, per corpus and gate state")
    print("=" * 78)
    alphas: list[float] = []
    gammas: list[float] = []
    for name, _org, _repo in corpus_specs:
        tarball = tarballs[name]
        for gate, delta_fraction in _GATE_STATES:
            cfg = {"tarball": str(tarball), "gate": gate, "delta_fraction": delta_fraction}
            r = _run_worker_subprocess("stage", cfg)

            files_delta_kb = r["after_files_kb"] - r["baseline_kb"]
            chunks_delta_kb = r["after_chunks_kb"] - r["after_files_kb"]
            vectors_delta_kb = r["after_vectors_kb"] - r["after_chunks_kb"]

            alpha = (files_delta_kb * 1024) / r["files_bytes"] if r["files_bytes"] else float("nan")
            gamma = (
                (chunks_delta_kb * 1024) / r["files_to_embed_bytes"]
                if r["files_to_embed_bytes"]
                else float("nan")
            )
            measured_d = (
                r["chunk_content_bytes"] / r["chunk_count"] if r["chunk_count"] else float("nan")
            )
            vector_kb_per_chunk = (
                vectors_delta_kb / r["chunk_count"] if r["chunk_count"] else float("nan")
            )

            _emit(
                corpus=name,
                gate=gate,
                baseline_kb=r["baseline_kb"],
                after_files_kb=r["after_files_kb"],
                after_chunks_kb=r["after_chunks_kb"],
                after_vectors_kb=r["after_vectors_kb"],
                files_delta_kb=files_delta_kb,
                chunks_delta_kb=chunks_delta_kb,
                vectors_delta_kb=vectors_delta_kb,
                files_count=r["files_count"],
                files_bytes=r["files_bytes"],
                files_to_embed_count=r["files_to_embed_count"],
                files_to_embed_bytes=r["files_to_embed_bytes"],
                chunk_count=r["chunk_count"],
                measured_d_bytes_per_chunk=round(measured_d, 1),
                vector_kb_per_chunk=round(vector_kb_per_chunk, 3),
                alpha_files_per_source_byte=round(alpha, 4),
                gamma_chunks_per_source_byte=round(gamma, 4),
            )

            if gate == "first-index":
                # alpha/gamma are properties of the whole-file materialization;
                # the first-index (gate-closed) run is the one where
                # files_to_embed IS files, exactly matching planning's §2.1
                # methodology (a whole-corpus measurement, not a narrowed one).
                alphas.append(alpha)
                gammas.append(gamma)
        print()

    print("=" * 78)
    print("stage 2: V_cap -- resident bytes for a fixed embedded-vector count")
    print("=" * 78)
    vcap_cfg = {"n": args.vcap_n, "dim": args.dim}
    vr = _run_worker_subprocess("vcap", vcap_cfg)
    vcap_delta_kb = vr["after_kb"] - vr["baseline_kb"]
    vcap_kb_per_chunk = vcap_delta_kb / vr["n"]
    structural_kb_per_chunk = args.dim * (8 + 24) / 1024  # 8B pointer + 24B PyFloat, per §2.2
    _emit(
        n=vr["n"],
        dim=vr["dim"],
        baseline_kb=vr["baseline_kb"],
        after_kb=vr["after_kb"],
        delta_kb=vcap_delta_kb,
        resident_kb_per_chunk=round(vcap_kb_per_chunk, 3),
        structural_kb_per_chunk=round(structural_kb_per_chunk, 3),
    )
    print()

    print("=" * 78)
    print("stage 3: N concurrent branch-index threads, extraction pool live")
    print("=" * 78)
    nconc_name = args.nconc_corpus or corpus_specs[-1][0]
    nconc_tarball = tarballs[nconc_name]
    print(f"# using corpus={nconc_name} tarball={nconc_tarball}, gate=first-index")
    n_values = [int(n) for n in args.n_threads.split(",")]
    for n in n_values:
        cfg = {
            "tarball": str(nconc_tarball),
            "n_threads": n,
            "gate": "first-index",
            "delta_fraction": 0.0,
        }
        nr = _run_worker_subprocess("nconc", cfg)
        _emit(
            corpus=nconc_name,
            n_threads=nr["n_threads"],
            n_processes=nr["n_processes"],
            baseline_self_kb=nr["baseline_self_kb"],
            after_self_kb=nr["after_self_kb"],
            self_delta_kb=nr["after_self_kb"] - nr["baseline_self_kb"],
            baseline_children_kb=nr["baseline_children_kb"],
            after_children_kb=nr["after_children_kb"],
            children_delta_kb=nr["after_children_kb"] - nr["baseline_children_kb"],
            elapsed_s=round(nr["elapsed_s"], 2),
            errors=len(nr["errors"]),
        )
        for err in nr["errors"]:
            print(f"#   error: {err}")
    print()

    print("=" * 78)
    print("summary")
    print("=" * 78)
    if alphas:
        _emit(
            alpha_avg=round(sum(alphas) / len(alphas), 4),
            alpha_min=round(min(alphas), 4),
            alpha_max=round(max(alphas), 4),
            n_corpora=len(alphas),
        )
    if gammas:
        _emit(
            gamma_avg=round(sum(gammas) / len(gammas), 4),
            gamma_min=round(min(gammas), 4),
            gamma_max=round(max(gammas), 4),
            n_corpora=len(gammas),
        )
    _emit(
        v_cap_resident_kb_per_chunk=round(vcap_kb_per_chunk, 3),
        v_cap_structural_kb_per_chunk=round(structural_kb_per_chunk, 3),
        v_cap_n=vr["n"],
        v_cap_dim=vr["dim"],
    )
    print(
        "# alpha/gamma above are from each corpus's first-index (gate-closed) run, matching "
        "the plan's §2.1 whole-file methodology. The recurring-1pct/10pct rows in stage 1 show "
        "the SAME alpha (files always materializes in full) alongside a much smaller "
        "chunks/vectors delta (files_to_embed narrows), which is the qualitative effect "
        "the plan's §0.2 and §3.3(b) describe."
    )
    print(
        "# vectors_delta_kb in stage 1 may run slightly high: _precompute_chunk_writer "
        "re-chunks internally (it has no seam to accept precomputed chunks), so that delta "
        "is 'new allocations since the chunks-term reading' rather than a pure vectors-only "
        "measurement. See the module docstring for why this is still a reasonable isolation."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
