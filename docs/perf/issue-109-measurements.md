# Issue #109 — re-derive worker, disk, and memory limits: measurements

**Unit convention, stated up front (mixing anchors is a real risk in this doc):**
every RSS/vector/memory-model figure below is **binary** (KiB/MiB = 1024-based),
matching `resource.ru_maxrss` (Linux: KB = 1024 bytes) and this repo's existing
`docs/perf/issue-108-measurements.md` convention. The one exception is
`MAX_EXTRACTED_BYTES = 2_000_000_000`, which is **decimal** (2 GB = 2,000,000,000
bytes) by the source constant's own definition — any comparison against it is
converted explicitly, never left implicit.

---

## 1. Environment

**Local box** (measurements in §2–§4): Linux, 12 cores, ~15.5 GB RAM, Python
3.12.13 (uv-managed `.venv`; the shell's own default is 3.14). `/tmp` is tmpfs
(7.8 GB) — disk-backed measurements used `/` (nvme0n1p2, 318 GB free) instead.

**Databricks dev serverless container** (§5, `M` and W4): read directly from
inside the deployed job via a temporary probe (job schedule stayed **PAUSED**
throughout; only manually-triggered one-time runs executed). Two separate
container instances were observed across two probe attempts, with a **>2x
spread** in reported memory — see §5.1.

---

## 2. AC2 — disk (E1): already correct on the base, verified not re-derived

Per the plan's §0.1, #106 already landed the disk half of #109. Verified by
citation, not rewritten:

- `indexer/fetch.py`: `REQUIRED_FREE_BYTES == MAX_TARBALL_BYTES == 500_000_000`.
- The guard message carries the real number (`need 500000000 ...`).
- `docs/runbooks/indexing-parallelism.md` §3 and `config.yaml` already read 0.5
  GB/worker (1→0.5, 2→1, 4→2, 8→4 GB).
- The tarball is the ONLY on-disk artifact (`indexer.ingest.iter_tar_source_files`
  streams in memory, never extracts) — confirmed by direct code reading, not a
  fresh sampling run (§0.1 forbids "restating a correct 8-worker figure" as new
  work).
- W3 (observed on the live dev job, both arms): `local disk at /tmp: 64.0 GB
  free of 89.1 GB total` — **disk is not a binding constraint at any allowed
  `index_concurrency` (up to 8, 4 GB peak)**.

**AC2: satisfied, unchanged.**

---

## 3. M1 — the memory model's coefficients (E3(b–f))

`scripts/measure_semantic_memory.py`, run against 4 corpora (this repo,
`flask`, `requests`, `django`), driving the REAL semantic path
(`iter_tar_source_files` → delta narrowing → `_precompute_chunk_writer`) with a
stub embedder returning **distinct** floats per chunk (per §2.2's trap — a
shared-cached-float stub understates resident vector cost ~4x).

| Corpus | alpha | gamma | d (bytes/chunk) |
|---|---|---|---|
| databricks-code-search | 2.1142 | 0.5571 | 2711.6 |
| flask | 2.044 | 0.4693 | 1644.2 |
| requests | 3.382 | 0.0 (see below) | 3747.7 |
| django | 1.5911 | 1.2627 | 1775.5 |

`requests`' gamma measured as 0 — a chunk delta small enough (chunk_count=413)
to fall below this box's RSS sampling granularity (allocator/page-granularity
noise), not evidence chunking is free for that corpus.

alpha: avg=2.2828, **max=3.382** (n=4). gamma: avg=0.5723, **max=1.2627** (n=4).
**Decision: use MAX-observed coefficients** (larger → smaller/safer derived
limits, larger/stricter `P_worst`) as the primary, conservative input; average
reported alongside for context (§8 judgement call, self-consistently applied
everywhere it's used). **`alpha + gamma = 4.6447`** (max of each, not the max
corpus's sum — the model treats them as independently-conservative).

`V_cap` (resident vector cost, 8000 × 1024 distinct-float vectors): **40.102
KB/chunk** (this session's re-measurement; close to planning's 40.8 KB/chunk —
both measure the same thing, structural is 32.0 KB/chunk exact
(`1024 × (8B pointer + 24B PyFloat)`), not "corrected" by this re-measurement,
per §2.2's own instruction not to).

`R_proc` = 121 MB/process (#108's own isolated measurement — reused, not
re-run, per the plan's L4).

`P_fixed`: a bare-interpreter floor probe gave 38,372 KB → +`import indexer.job`
(pulls SQLAlchemy/databricks-sdk) → 63,048 KB → +a throwaway SQLAlchemy engine →
67,628 KB. This is a **floor** (~68 MB) — it excludes the real pool_size-scaled
connection pool and Databricks SDK client state a live job carries. **Adopted
P_fixed = 300 MB** (the plan's own conservative worked-example value), noting
the ~68 MB floor as a cross-check, not a replacement.

### 3.1 A methodology finding not anticipated by the plan: fork-time COW RSS contamination of `RUSAGE_CHILDREN`

M1's stage-3 (N-concurrency) sweep reported `after_children_kb` **numerically
identical** to `after_self_kb` at every N (e.g. N=1: self=1,046,984,
children=1,046,984). Root-caused directly (scratch scripts, not committed):
when the extraction pool's `spawn` workers are forked/exec'd **after** a
repo-worker's `_precompute_chunk_writer` has already ballooned the calling
thread's RSS to ~1 GB — exactly production's real call order in
`indexer/job.py` — each child's `ru_maxrss` (read later via `wait()` /
`RUSAGE_CHILDREN`) captures the **fork-time COW snapshot of the parent's
then-current RSS**, not the child's real post-exec working set. Verified
directly: draining the pool BEFORE ballooning gives children ~144–150 MB
(matching #108's own R_proc ~121 MB order of magnitude); draining AFTER gives
children ≈ self's contemporaneous value — a ~7–9x inflation with **no
corresponding real memory pressure** (COW pages are shared, billed once by the
cgroup, not per-process).

**Implication:** this affects M1's own stage-3 "children" column, and by the
same mechanism, the production `peak rss: self=... children=...` instrumentation
on Arms A/B (`indexer/job.py`'s new n5 log line) whenever the pool is
(re)spawned after chunk-writer inflation. It is a `ru_maxrss` **measurement
artifact**, not evidence of doubled real memory. The `P_worst` model itself is
unaffected in its primary form because `R_proc` is sourced from #108's own
isolated measurement, not from this contaminated figure — but Arm A/B's
observed `children=` numbers below should be read as **upper bounds, not
literal per-process costs**.

Stage-3 self-deltas (uncontaminated — `self` reflects only the process's own
allocations) at N=1..4, worst-case corpus (django, first-index/gate-closed):
N=1: 983,036 KB; N=2: 1,828,428 KB; N=3: 2,582,480 KB; N=4: 3,216,572 KB —
**sub-linear**, consistent with page-cache/tarball-decompression sharing across
threads, not a red flag.

**Caveat on these specific numbers (found and fixed post-hoc in review, not
re-measured):** at measurement time, `scripts/measure_semantic_memory.py`'s
stage-3 worker discarded `_precompute_chunk_writer`'s return value before
calling `pool.stream()`, so each thread's vectors were collectable before (or
concurrently with) sibling threads' peaks — understating true N-way
concurrent residency relative to production, which holds `chunk_writer` alive
across the whole write window. The script is fixed in this PR (the return
value is now held alive across `pool.stream()` and explicitly `del`eted after,
matching stage 1's pattern) for future use, but the N=1..4 numbers above
**were not re-measured** against the fix, since **no decision in this PR rests
on them** — the adopted N=4 decision comes from the `P_worst` model (§6) and
the real Arm A/B live-job runs (§8–§10), not from this local sub-measurement.
Treat the sub-linearity finding above as directional, not load-bearing.

---

## 4. B — the three anchors

- **`B_prod`** (real corpus, unnesting `files.branches`): top row `repo_id=46`
  (`IceRhymers/opencode`, branch `dev`), `src_bytes = 31,778,187` (~30.3 MiB).
- **`B_obs`** (measurement corpus, re-measured directly): `opencode@dev` =
  33,015,231 bytes decoded source (18,617 chunks, 4,759 files) — slightly
  higher than `B_prod` (encoding/whitespace differences between the two
  measurement paths).
- **`B* = max(B_prod, B_obs) = 33,015,231 bytes ≈ 31.49 MiB`** — the anchor used
  for every decision below.
- **`B_ceil = MAX_EXTRACTED_BYTES = 2 GB`** — theoretical, loose, **never** used
  to drive a decision (only to illustrate why a naive `B_ceil`-anchored model
  would falsely condemn the status quo — see below).

---

## 5. `M` — the container memory ceiling (E3(a))

Two container instances observed across two probe attempts on the same job:

| Attempt | task_run_id | `cgroup_v1_memory.limit_in_bytes` | `sched_getaffinity` | Outcome |
|---|---|---|---|---|
| 1 | 523922694112794 | 8,385,462,272 (~7996 MiB) | 4 | **OOM-killed** during the allocate-bracket, last logged step `cumulative_mb=12800` |
| 2 | 89875228623550 | 24,706,547,712 (~23.0 GiB), `MemTotal` 32,264,556 kB | 4 | Ran its bracket to a designed 16,384 MB cap without dying (never exercised further) |

**`cgroup_v2_memory.max`/`memory.high` both unreadable** (`FileNotFoundError`)
on this runtime — the repo's own `extract_pool.py::_cgroup_cpu_quota()` v2-only
assumption does not hold for `memory.max`; the v1 fallback
(`/sys/fs/cgroup/memory/memory.limit_in_bytes`) was required.

**Attempt 1's cgroup read was demonstrably a MISREAD**: the container died at
`cumulative_mb=12800` (the bracket's last logged step before the kill), i.e.
the real ceiling sits in `(12800, ~13056] MiB` — **~60% higher** than the
cgroup-reported ~7996 MiB.

**`M = 12800 MiB`** (the smaller, real, empirically-grounded ceiling from
attempt 1 — chosen conservatively over attempt 2's larger, undead container,
per the resumption brief's instruction to use the smaller real ceiling for
safety). **`0.7 × M = 8960 MiB`** — the budget used throughout.

**`W4` (container CPU count) = 4** (`len(os.sched_getaffinity(0))`, both
attempts agree) — sourced from this probe, per the plan's design, breaking the
apparent circularity between E4 (needs W4) and E5 (Arm B, which would
otherwise be W4's only source).

### 5.1 First-order finding: container sizing is unstable across attempts

A **≥2x spread** in effective memory ceiling was observed between two
instances of the *same* job on *unspecified* dev serverless compute (~8 GiB vs.
~23.0 GiB reported; real ceilings both plausibly larger than reported). This is
reported as a finding, not resolved — the smaller, conservative number is what
every downstream decision uses.

---

## 6. `P_worst` — the model, evaluated at the standard cap

```
P_worst(N) = N × max( (alpha+gamma)·B_breach,
                      (alpha+gamma)·(d×C) + V_cap·C )
             + extract_processes × R_proc + P_fixed
```

At the **standard/default global chunk cap `C = 8000`** (ordinary unmodified
production, NOT the measurement corpus's per-repo overrides — see §7 for why
those diverge), with the MAX coefficients above (`alpha+gamma = 4.6447`,
`d = 3747.7`, `V_cap = 40.102 KB/chunk`, `R_proc = 121 MB`,
`extract_processes = 4` — confirmed live in the priming log's "symbol
extraction: 4 process(es)"), `P_fixed = 300 MB`:

- Breach term at `B* = 31.49 MiB`: `(a+g)·B* ≈ 146.3 MiB`/worker.
- Under-cap term at `C = 8000` (the regime that wins under the standard cap —
  vectors dominate the uncapped terms ~8x at the cap, per the plan's §2.3):
  `(a+g)·d·C ≈ 132.8 MiB` + `V_cap·C ≈ 313.3 MiB` = **446.1 MiB**/worker.
- `max(146.3, 446.1) = 446.1 MiB`/worker.

`P_worst(3, B*) = 3×446.1 + 4×121 + 300 = 2122.3 MB`
`P_worst(4, B*) = 4×446.1 + 4×121 + 300 = 2568.4 MB`

Both `<< 0.7M = 8960 MiB` (margin ~76% at N=3, ~71% at N=4). Cross-checked with
AVERAGE coefficients (`alpha+gamma = 2.855`, avg `d = 2469.75`):
`P_worst(4) = 2252.4 MB` — same conclusion; **not sensitive to the max-vs-avg
coefficient choice.**

**Illustrative-only counter-example (never used to decide anything): at
`B_ceil = 2 GB` (= 1907.3 MiB binary)** the breach term is
`(alpha+gamma)·B_ceil = 4.6447 × 1907.3 ≈ 8859.1 MiB`/worker — the breach
regime wins by **≈19.9x** over the under-cap term (446.1 MiB, §6), giving
`P_worst(2, B_ceil) = 2×8859.1 + 484 + 300 ≈ 18,502 MiB` — i.e. the model
would falsely condemn TODAY's clamp=2 status quo many times over if evaluated
at the loose theoretical bound instead of the real observed `B*`. This is
exactly why §3.3(c) of the plan anchors the decision on `B*`, never `B_ceil`.
(The planning-stage worked example in the plan document itself used its own
pre-`M1` coefficients, ~2.35 rather than the measured 4.6447, and got a
smaller — still condemning — ~10.2 GiB; both versions support the same
qualitative point, so both numbers are recorded here for provenance:
whichever coefficient set is used, `B_ceil` is not a fit substitute for `B*`.)

**Decision (model-only, pre-Arm-A): branch (i), RAISE — both N=3 and N=4 clear
`0.7M` with large margin. Adopt N=4 (the largest passing N), contingent on Arm
B completing cleanly** — modelling alone is not sufficient per the plan's AND
condition.

---

## 7. Re-evaluation against Arm A's own observed peak (§3.3(c).iii)

The standard-cap model above does **not** reflect what Arm A actually ran,
because the measurement corpus's per-repo chunk-cap overrides (opencode =
22,340, ≈2.8x the standard 8000 cap — see §9) make this specific corpus far
more memory-expensive than "standard production": Arm A's real self+children
was ~4.75x higher than the standard-cap model's own N=2 prediction (1676 MiB),
because the override intentionally lets opencode use ~874.9 MiB of vectors
instead of breaching into the cheap ~146 MiB breach regime. Expected (flagged
in the plan's §3.2 as a likely consequence), not a bug — but it means only
Arm A's own empirical number, not the standard-cap model, can gauge Arm B's
real risk on this corpus.

---

## 8. Arm A — before (clamp=2, TODAY's shipped code)

Both runs: 22 repos resolved (19 retained + 3 explicit), `symbol extraction: 4
process(es) (spawn); pool preflight ok`, `local disk ...; 2 worker(s) x 0.5 GB
peak`, `semantic enabled: clamping index_concurrency 4 -> 2`, 22/22 branches ok,
0 skipped/conflicts/failed, 0 repos purged, no `degraded semantic coverage`
WARNING, no 429/retry lines.

| | Run 1 (`489590218152767`) | Run 2 (`19039699744754`) |
|---|---|---|
| wall (execution_duration) | 350.5s | 342.4s |
| `peak rss: self=... children=...` | self=6,930,768 KB children=1,247,176 KB | self=7,059,172 KB children=1,252,284 KB |
| self+children | 8,177,944 KB ≈ **7986.3 MiB** | 8,311,456 KB ≈ **8116.7 MiB** |
| % of `0.7M` (8960 MiB) budget | 89.1% | 90.6% |

The two runs agree within 1.6 percentage points — **not a fluke: at TODAY's
clamp=2, this corpus already consumes ~89–91% of the safety budget.** Best-of
(faster wall): run 2, 342.4s. Worse-of (higher memory, used as the conservative
anchor going forward): run 2, 8116.7 MiB.

**§3.3(c).iii re-evaluation:** this real number is ~4.75x the standard-cap
model's prediction (§7) — the standard-cap model cannot gauge Arm B's risk on
this corpus. Direct reasoning from Arm A's own peak: this 22-repo corpus has
exactly 3 memory-heavy repos (opencode ~874.9 MiB vectors, claw-code ~147.9 MiB,
nanoclaw ~30 MiB), and overlap is capped by there being only 3 of them
regardless of N — so N=3/N=4 were estimated to land close to Arm A's own
number (perhaps +50–150 MiB), i.e. plausibly under budget but with a **thinner
margin (~85–90% utilized)** than the standard-cap model implied. **Decision:
still target N=4** for the single Arm B attempt (the largest of {3,4}, and
nothing in the refined reasoning favors N=3 specifically), with full awareness
of the thinner real margin and a non-negligible chance of failure — a valid,
reportable outcome either way per §6, not a trigger to retry at a different N.

---

## 9. Arm B — after (clamp=4, the derived change)

Applied the one-line change (`indexer/repo_config.py::effective_workers`:
`min(index_concurrency, 2)` → `min(index_concurrency, 4)`), redeployed
(`databricks bundle deploy -t dev`), cleared stamps, warmed Lakebase (>300s
`SELECT 1` loop immediately before each timed run — see §10 for a warm-up
reliability note), ran twice.

Both runs: 22/22 branches ok, 0 skipped/conflicts/failed, 0 repos purged,
**no** `semantic enabled: clamping ...` line (correct: `index_concurrency=4`
now equals the clamp, so `effective_workers` is a no-op passthrough — matching
§11's conclusion that the `index_concurrency` default itself did not need to
move), `local disk ...; 4 worker(s) x 0.5 GB peak`, `symbol extraction: 4
process(es) (spawn); pool preflight ok`, **zero** WARNING/ERROR/retry/429 lines
anywhere in either 191-line log.

| | Run 1 (`752462522914821`) | Run 2 (`785234477657138`) |
|---|---|---|
| wall (execution_duration) | 307.8s | 305.5s |
| `peak rss: self=... children=...` | self=6,128,880 KB children=1,445,408 KB | self=6,225,624 KB children=1,429,280 KB |
| self+children | 7,574,288 KB ≈ **7396.8 MiB** | 7,654,904 KB ≈ **7475.5 MiB** |
| % of `0.7M` (8960 MiB) budget | 82.6% | 83.4% |

Both runs agree within 1 percentage point — not a fluke. Best-of (faster
wall): run 2, 305.5s.

**Surprising but real: Arm B's total peak is LOWER than both Arm A runs, and
its margin (~17%) is MORE comfortable than Arm A's own (~9–11%).** `ru_maxrss`
is a same-process high-water mark, so this is a genuine peak-memory
observation, not a modelling artifact — Arm B's own `self` component (6.13–6.23
million KB) is genuinely lower than either Arm A `self` value (6.93–7.06
million KB). Consistent with (not contradicting) §8's reasoning: this corpus
has only 3 memory-heavy repos, and with 4 workers instead of 2 their embedding
windows are *less* likely to bunch up at the tail (more workers drain the
22-repo queue faster and spread the 3 heavy repos across more concurrent slots
with shorter individual overlap) — a property specific to this corpus's
repo-size distribution, **not** a general "N=4 always costs less than N=2"
claim.

**No stop condition (§6 of the plan) was triggered anywhere in the Arm B
sequence.**

## 10. AC1 — before/after table

| | Arm A (clamp=2, best-of wall) | Arm B (clamp=4, best-of wall) |
|---|---|---|
| wall | 342.4s | 305.5s (**−10.8%**) |
| peak self+children | 8116.7 MiB (90.6% of budget) | 7475.5 MiB (83.4% of budget) |

**FINAL DECISION (AC1 + AC3): adopt N=4.** Both the `P_worst` model (§6, before
any arm ran) and two clean, mutually-consistent empirical Arm B runs (§9) agree.

**Lakebase CU state**: `resources/lakebase.yml` pins
`autoscaling_limit_min_cu: 0.5`, `autoscaling_limit_max_cu: 4`,
`suspend_timeout_duration: 300s`. Warmed with a `SELECT 1` loop for the full
300s+ immediately before every timed run (priming, Arm A ×2, Arm B ×2 — 5
warm-ups total), confirmed complete each time via elapsed wall-clock (302.3s /
301.6s measured, not assumed).

**Budget/429 posture**: 5 full semantic-index runs total (1 priming + 2 Arm A +
2 Arm B) against the real, paid AI Gateway embedding endpoint. **Zero** 429s or
`databricks.sdk.retries` entries observed in any run's log; zero `degraded
semantic coverage` WARNINGs.

---

## 11. `index_concurrency` default (E4/M2 — the ingest-thread-scaling question)

`scripts/measure_ingest_threads.py`, repos: fastapi, sqlalchemy, sympy, django.

- N=2 idle: 1.26x (ambiguous band, ≥1.6 parallelizes / ≤1.1 doesn't).
- N=4 idle: **1.20x** (NO-SCALE — ≤1.2 threshold, exactly at the boundary).
- N=2 pool-live: 1.20x (ambiguous).
- N=4 pool-live: **1.15x** (NO-SCALE).
- Component breakdown confirms the GIL-bound prior (§0.4 of the plan):
  `tf_next`/`fh_read` cumulative time grows super-linearly with N (contention);
  `decode` stays roughly flat — consistent with a mostly GIL-held pass.

**Per the plan's §3.3(d) rule 2** ("the default may rise to the smallest value
≥ N that M2 shows a gain for" once the clamp itself rises): M2 shows **no**
real gain at 4 threads. **`index_concurrency`'s default stays 4** — and no
change was even needed: it was already 4 (`config.yaml`'s commented-out
default), so raising the semantic clamp from 2 to 4 alone makes
`effective_workers = min(4, 4) = 4` with zero separate change to
`index_concurrency` itself.

---

## 12. The two derived byte limits (§3.3(f)) — documented, neither changed

Both solved at the **pinned N=2** operating point (methodological, to break the
circularity of solving for `B` from a model whose dominant term is `B` — not a
claim that N=2 is the adopted concurrency, which is 4 per §10).
`RHS = 0.7M − extract_processes·R_proc − P_fixed = 8960 − 484 − 300 = 8176 MiB`.

**(f1) `MAX_EXTRACTED_BYTES` (breach regime, V=0):**
`B ≤ RHS / (N·(alpha+gamma)) = 8176 / (2×4.6447) ≈ 880.1 MiB ≈ 922.9 MB
(decimal)`. Current value: `2_000_000_000` bytes = 1907.3 MiB (decimal 2 GB).

**(f2) The chunk cap `C` (under-cap regime):**
`C ≤ RHS / (N·((alpha+gamma)·d + V_cap))`. Denominator =
`4.6447×3747.7 + 41,064.4 B ≈ 58,471 B ≈ 57.1 KiB`/unit-of-cap. `C ≤
8176×1024×1024 / (2×58,471) ≈ 73,311 chunks`. Current global default: 8000
(**~11% of the derived bound** — nowhere near binding).

**Neither value was changed.** `B* = 31.49 MiB` is only **3.6%** of the derived
`f1` bound (880.1 MiB) — no branch in the measurement corpus approaches either
the current 2 GB constant or the derived ~880 MiB one, so this run gives **no
empirical signal** to justify lowering a constant whose breach fails the branch
and closes the whole run's reconciliation checkpoint (`job.py`'s
`_decide_reconciliation` requires `failures == 0`). Per the plan's explicit
fallback ("otherwise document the derived value and keep 2 GB with a
comment"), both derived values are recorded here and in the source comments
(`indexer/ingest.py`, `config.yaml`) for the next time this needs re-deriving,
but the live constants are unchanged. The chunk cap `C=8000` also stays —
it uses only ~11% of its own derived budget, so it was never a candidate for
change either.

**Which regime binds at `B*`:** the **breach** regime, at the standard cap.
`B*` (~31.49 MiB, opencode@dev's real source size) exceeds `d×C` at the
standard `C=8000` under either the conservative modelling `d` (3747.7 B/chunk
→ threshold ≈28.6 MiB) or opencode's own real chunking density (≈1773
B/chunk → threshold ≈13.5 MiB) — i.e. **a repo the size of `B*` would actually
breach the standard 8000-chunk cap**, which is exactly why the measurement
corpus needed opencode's per-repo override (22,340, §9's context) to avoid
degrading it during Arm A/B. This makes `f1` (the breach-regime derived bound,
not `f2`) the relevant limit to check `B*` against — already done above:
`B*` is only 3.6% of `f1`'s ~880 MiB, comfortably clear. `f2`'s under-cap
regime governs a *different* class of repo: one that legitimately reaches the
cap without a bigger override (§6's worked "vectors dominate ~8x" example),
which no repo in this measurement corpus represents at the standard cap.

---

## 13. E7 — the connection-pool property, confirmed and pinned

`pool_size == effective_workers` (raised to 4), `max_overflow=0`. Holds by
**sequencing, not construction**: `indexer/job.py`'s `with engine.connect() as
shas_conn:` (the advisory `shas_fn` read, #104) opens and closes a second,
short-lived connection strictly BEFORE `_precompute_chunk_writer`/embedding —
confirmed by direct code reading (job.py:1298 area) and pinned by a new test,
`tests/unit/test_job.py::test_shas_fn_connection_closes_before_embedding_starts`,
which counts currently-open connections and asserts exactly 1 while `shas_fn`
runs and 0 by the time embedding starts. No live-job evidence of Lakebase
connection pressure at N=4 (no `QueuePool` timeouts in either Arm B run), but
the ceiling itself did double (2→4 concurrent connections on the semantic
path) — worth knowing if a future Lakebase compute-size change is on the table
alongside a semantic-path concurrency change.

Also found (documented, not a blocker): `embedding_concurrency` at its
`le=8` config ceiling combined with the new clamp of 4 now yields `4×8=32`
in-flight gateway requests — **exceeding** the SDK's 20-connection pool for
the first time (`2×8=16` stayed under it before). `pool_block=True` means this
degrades to silent serialization, not an error, so it is not a correctness
issue, but is flagged in `config.yaml`, `app/config.py`, and both runbooks.

---

## 14. The semantics tripwire (§5.1 of the plan)

`tests/unit/test_semantics_version_tripwire.py::test_semantics_change_bumps_the_index_semantics_version`
**fails on this PR**, as expected and pre-documented by the plan: `SEMANTICS_PATHS`
includes `indexer/ingest.py`, which this PR touches (the `MAX_EXTRACTED_BYTES`
comment, §12) and which — per the test's own module docstring — is *added*
relative to `origin/master` locally (postdates master, landed by #106,
unmerged past this integration branch), producing an expected false positive
this test's own docstring names verbatim. **Per the plan's §5.1, this is
explicitly NOT a stop condition.** `INDEX_SEMANTICS_VERSION` was **not**
bumped and `indexer/ingest.py` was **not** removed from `SEMANTICS_PATHS` —
this section is that required "say so in the PR."

---

## 15. Limitations

- **Prod** is out of scope and unreachable; dev serverless is the labelled
  proxy throughout.
- **Corpus scale**: the real dev corpus (19 repos, 1142 files) is far too
  small on its own; the measurement corpus (+opencode/nanoclaw/claw-code) is a
  labelled proxy, not production traffic.
- **`M`'s instability** (§5.1): a ≥2x spread was observed between two container
  instances of the same job. The smaller, conservative reading was used
  throughout; the real production ceiling could be substantially larger.
- **`RUSAGE_CHILDREN` COW contamination** (§3.1): Arm A/B's `children=` figures
  are upper bounds, not literal per-process costs, whenever the pool is
  (re)spawned after chunk-writer inflation (the real, unavoidable production
  call order).
- **`B*` is a sample max** over an executor-chosen corpus (§8.3 of the plan):
  passing at N=4 on this corpus is necessary, not sufficient evidence for
  every possible future repo.
