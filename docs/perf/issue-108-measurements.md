# Issue #108 — process-pool symbol/edge extraction: measurements

Two measurement rounds, per the plan's §8.4 requirement. The **planning-time**
probe (§1) established the initial GO decision, against an on-disk-walk baseline
that no longer exists in production. The **shipped** measurement (§2) re-runs the
comparison through the real `indexer.ingest.iter_tar_source_files` tarball source
via `scripts/measure_extraction_pool.py`, and is the number this PR reports as
AC1's evidence — per the plan, it supersedes §1 rather than sitting beside it as
an equal alternative.

**§2's result requires a stop-and-report, not a silent GO**: see §2.3.

## 1. Planning-time probe (superseded — on-disk-walk baseline)

Throwaway `/tmp` probes, not committed to the repo. **Environment:** Linux, 12
cores, Python 3.12 (`.venv`), `spawn`, 2 MB batches. **Corpus:** this repo
including `.venv/` at planning time — 9605 indexable files / 97.4 MB, 5064 files
/ 65.8 MB qualifying.

| Path | Wall clock | Speedup |
|---|---|---|
| serial in-process (on-disk walk) | 9.84 s | 1.00x |
| pool, 2 processes | 5.45 s | 1.81x |
| pool, 4 processes | 2.91 s | **3.38x** |
| pool, 8 processes | 2.06 s | 4.78x |

Output parity `identical=True` at every process count. This number used a
**batch-submit harness** (`executor.map` over a pre-built list), not the shipped
bounded-look-ahead generator, and a source that read from an already-extracted
tree — both superseded by #106's single-pass tarball source. Recorded here for
provenance only; **do not cite as AC1's evidence.**

## 2. Shipped measurement (real tarball, real `stream()`)

`scripts/measure_extraction_pool.py --tarball repo.tar.gz --processes 2,4,8
--repeat 3`, driving the actual `ExtractionPool.stream()` a production branch
calls, over `indexer.ingest.iter_tar_source_files`.

**Environment:** Linux, 12 cores (`os.sched_getaffinity`; no cgroup-v2 quota on
this box), Python 3.12.13 (`.venv`), `spawn`, 2 MB batches (the default
`_BATCH_BYTES`).

**Corpus:** a real GitHub-codeload-shaped tarball of this repository's own
working tree at `5f290c6` (the branch point), including `.venv/` as a
site-packages-heavy large-repo proxy (`.venv/bin`'s one absolute-target symlink
excluded — `iter_tar_source_files` correctly rejects it, matching production
behavior for such a member). 4547 indexable files / 54.5 MB, of which **2749
files / 45.7 MB qualify for parsing** (60% of files, 84% of bytes — a Python-
and-JS-heavy tree). Smaller than the planning-time corpus (the repo has grown
since, and `.venv/bin` is excluded), but the same shape of proxy.

### 2.1 Result

```
$ uv run python scripts/measure_extraction_pool.py --tarball repo.tar.gz --processes 2,4,8 --repeat 3
loading repo.tar.gz ...
4547 indexable files / 54.5 MB, 2749 qualify for parsing / 45.7 MB (ingest: 0.79s -- serial in the parent, both arms below)

          path   extract_s     total_s   speedup       note
        serial       6.563       7.355     1.00x
       pool x2       3.423       4.215     1.74x  identical
       pool x4       1.913       2.705     2.72x  identical
       pool x8       1.422       2.214     3.32x  identical

ingest (serial, shared by both arms): 0.79s of 7.35s serial total (11%) -- the floor AC1's 'combined' speedup above cannot cross, however many processes extract_processes uses.
```

Reproduced across three separate invocations (best-of-3 per data point each
time); the pattern is stable, not noise:

| Run | pool x2 | pool x4 | pool x8 |
|---|---|---|---|
| repeat=1 | 1.81x | 2.75x | 3.22x |
| repeat=3 (a) | 1.73x | 2.75x | 3.22x |
| repeat=3 (b, tabulated above) | 1.74x | **2.72x** | 3.32x |

Output parity: **`identical=True` at every process count, every run** — the
pooled result list compared element-wise against the serial `extract_file` list.

### 2.2 Where the two numbers in the table come from

- **`extract_s`** — the pool's own wall clock (`ExtractionPool.stream()` alone),
  comparable to the planning-time probe's number.
- **`total_s`** — `extract_s` plus the ONE shared `ingest` cost (identical in
  both arms, since both read the same pre-loaded `ParsedFile` list): this is the
  number that maps onto a real branch's `phase timing … parse=…` field, because
  `_timed_items` charges `ExtractionPool.stream()`'s pull time — which includes
  blocking on a future — entirely to `parse` (proven by
  `tests/unit/test_job.py::test_pool_engaged_attributes_stream_production_to_parse`,
  T10). **`total_s`'s speedup is AC1's evidence**, not `extract_s`'s.

`extract_s` alone clears 3x at 4 processes (6.563 / 1.913 = **3.43x**, ~86%
parallel efficiency for the CPU-bound work). `total_s` does not, because ingest
is a **fixed, unavoidably serial** 0.79s added to both the numerator and
denominator (Amdahl's law: at an 11% serial fraction, even the CPU-bound part
scaling perfectly to infinity caps combined speedup at 1/0.11 ≈ 9.1x, and at 4x
*ideal* extraction speedup the combined ceiling is 1/(0.11 + 0.89/4) ≈ 3.03x —
so the measured 2.72x reflects real, good parallel efficiency running into a
structural ceiling, not a bug in this implementation).

### 2.3 The go/no-go decision: STOP AND REPORT, per §8.4

**AC1 ("≥3x on 4+ cores") is NOT met at 4 processes on the shipped measurement:
2.72–2.75x combined, reproduced across three runs.** At 8 processes it clears
the bar (3.22–3.32x), but AC1 asks for the bar to be cleared starting at 4, not
only eventually at 8.

Per the plan's binding instruction (§8.4, R12), this is the exact condition
under which the executor must **stop and report to the operator rather than
quietly shipping a change whose stated acceptance criterion is a measurement it
missed**, and must not retune the corpus until the number cooperates. It has not
been retuned: this is the corpus described in §2's "Corpus" paragraph, measured
as found.

This is a real, structural finding, not a defect in the pool's own parallel
efficiency (§2.2's Amdahl arithmetic shows the CPU-bound part scales well). It
is a consequence of #106: the file source is now a single serial
gzip-decompress + decode + filter pass that this pool cannot touch (§4.7.1 of
the plan), and on this corpus that pass is 11% of the serial total — enough to
keep the *combined* number under 3x at 4 processes even though the *extraction*
number alone clears it comfortably (3.43x). The plan names the likely
implication directly: *"it may mean the win now lives in parallelizing
*ingest*, which is a different issue."*

**This blocks a clean GO claim for AC1 as literally worded.** The design itself
(D1–D8: fork-safety, `BrokenProcessPool` blast-radius containment, the
terminable preflight probe, bounded look-ahead, order preservation) is sound and
independently valuable — a shared process pool doubles-plus extraction
throughput at 4 processes and more than triples it at 8, which is a real win for
any dominant-repo run — but AC1's specific "≥3x on 4+ cores" bar is not met at
the "4" endpoint on this corpus. Reported here rather than adjusted to look
better.

## 3. Serial-fraction split (for #109)

The number that tells #109 whether raising `extract_processes` past 4 is worth
anything at all: **ingest is 0.79s of the 7.355s serial total (≈11%) on this
corpus.** #109 should treat ~9x as this pool's asymptotic combined-speedup
ceiling on a similarly-shaped corpus, not the naive "N cores → Nx" expectation.
