# Runbook: parallel indexing

The indexing job (`code-search-index`) works on several repos at once, sized by
`index_concurrency` in the central `config.yaml`. This runbook covers the
properties an operator needs at 2am: what a killed run costs, how to read the
new log lines, how much disk the concurrency actually buys and burns, and how to
force a re-index.

---

## 1. A killed run is safe. This is the headline property.

**There is no checkpoint file and no resume flag, and none is needed.**

Each repo's provenance stamp — `(last_indexed_commit, index_semantics_version)`
— is written inside *that repo's own transaction*. So a run killed halfway
leaves every completed repo durably stamped and every incomplete repo untouched.
Re-running the job indexes exactly the remainder: the completed repos are
skipped before their tarballs are fetched.

Practical consequences:

- Killing a run mid-flight is a safe operation. Do it.
- Re-running after a partial failure is cheap, not a full re-index.
- A repo that fails does not fail the run's other repos. The run exits non-zero
  with the failure counted, and the healthy repos are indexed.

The completion line reports all four outcomes:

```
INFO indexer.job [-]: indexing complete: 12 ok, 40 skipped, 0 conflicts, 1 failed (of 53) in 631.4s
```

`conflicts` are **not** failures and do not affect the exit code — but read this
carefully, because the name understates it: **a conflicted repo was rolled back
and is NOT indexed.**

The `repos` row changed while that worker held its transaction, so the whole
transaction (files, symbols, chunks, the sweep) was discarded.

**If you are seeing this in production, something is wrong that this runbook did
not anticipate — do not treat it as routine.** No known writer can reach it.
`index_repo`'s first statement is an `ON CONFLICT DO UPDATE` that takes the
`repos` row lock and holds it until commit, so a competing writer either blocks
until the worker finishes (its write lands *after* the guard) or commits first
(and the worker's baseline is then *its* value). Both directions were measured
against real Postgres: a concurrent `UPDATE ... SET index_semantics_version =
NULL` blocked for the worker's entire transaction and the guard still matched.
**In particular, running the §5 force-reindex while a run is in flight does NOT
cause this** — an earlier version of this runbook said it did, and that was
wrong.

What it is actually for: it fires loudly if `for_each_task` sharding lands, or if
someone raises `max_concurrent_runs` in `resources/job.yml`. Either removes the
single-writer property above, and this is the guard that says so instead of
silently restoring stale content.

It is excluded from the exit code because it **self-heals** — the next run sees a
stamp it does not match and re-indexes that repo. If you cannot wait for the next
scheduled run, re-run the job; completed repos are skipped, so the retry is
cheap. Then work out which writer got there, because per the above there should
not be one.

---

## 1.1 One logical corpus writer: why runs are now serialized

`resources/job.yml` pins `code_search_index` to `max_concurrent_runs: 1`,
retaining `queue: enabled: true`. An overlapping trigger (a scheduled run
landing on top of a manual retry, or two manual runs) now queues instead of
starting a second concurrent run.

**What this invariant actually buys:** at most one run of this job is ever
fetching, deriving, or applying corpus state at a time. That is a
precondition for global desired-state reconciliation (#56), which performs
corpus-wide destructive DML (deleting rows for repos/branches no longer in
the desired inventory) — safe only if no second run can be deriving a
different desired inventory concurrently and racing the sweep.

**What it is NOT:** a replacement for the per-branch sequencing in §1 above.
The two invariants operate at different grains:

| Invariant | Grain | Enforced by |
|---|---|---|
| Branches within one repo are sequential | inside one run, one repo | `indexer/job.py`'s per-repo worker loop (never concurrent) |
| At most one job run at a time | across runs | `resources/job.yml`'s `max_concurrent_runs: 1` |

**Coverage boundary — read this before adding a second writer.** This
invariant covers only concurrent *runs of this job*. It does NOT cover:

- A second job, script, or ad hoc process that writes to the same corpus
  tables outside `code_search_index`.
- A future per-repo/per-branch task-sharding split (`for_each_task`) that
  fans a single run's work across *separate job runs* rather than threads
  within one run's `ThreadPoolExecutor` — that would multiply run count and
  defeat the pin's purpose entirely.
- Raising `max_concurrent_runs` above 1 — the guard `indexer/store.py`'s
  `StaleIndexError` exists specifically to fail loudly if this invariant is
  ever silently removed (see §1 above).

Any of these needs a shared database fencing/lease protocol (an advisory
lock, a lease table) before it can safely coexist with reconciliation's
corpus-wide DML. Until that protocol exists, treat "raise
`max_concurrent_runs`" or "add a second writer" as a designed-around case,
not a config tweak.

---

## 2. Reading the logs

Every record carries the repo it belongs to in brackets, including records from
`indexer.fetch`, `indexer.store`, and `app.embed`, which have no repo name
of their own. Records emitted outside a worker (config resolution, the drain
loop, third-party libraries) carry `-`.

```
INFO indexer.job [-]: local disk at /tmp: 41.2 GB free of 64.0 GB total; 4 worker(s) x 0.5 GB peak
INFO indexer.fetch [acme/widgets]: ...
INFO indexer.job [acme/widgets]: phase timing acme/widgets@main: total=204.92s resolve=0.00s download=12.10s parse=31.00s embed=88.20s db=64.50s sweep=0.30s other=8.82s
INFO indexer.job [acme/widgets]: finished acme/widgets in 205.34s (resolve=0.42s list=0.00s)
INFO indexer.job [acme/gadgets]: skipped acme/gadgets@main: already indexed at abc123 (semantics v1) in 0.41s
```

**To find the giant:** grep for `finished .* in` and sort by the elapsed number.
Wall-clock for the whole run is bounded below by the single slowest repo, so if
one repo dominates, raising `index_concurrency` will not help — that is Amdahl's
law asserting itself at the repo level, and the fix is to exclude the repo or
accept the duration.

**To decide whether tuning is worth it:** compare the total on the completion
line against the sum of the per-repo elapsed times. If the total is already
close to the slowest single repo, the pool is not the bottleneck.

### 2.1 Finding the dominant phase, not just the dominant repo

Every **indexed** branch emits one `phase timing` line accounting for its entire
wall clock. Skipped, failed, and conflicted branches emit none — there is nothing
to attribute.

```
grep 'phase timing' run.log            # one line per indexed branch
```

The eight fields are fixed, always present, always in this order, always `%.2fs`.
A phase that did not run prints `0.00s` rather than disappearing, so the line
never changes shape between a semantic-on and a semantic-off run and every grep
you write keeps working. Read the largest field; that is the branch's bottleneck.

(There used to be a ninth, `extract=`. #106 removed the phase itself — the
tarball is streamed once, in memory, and never extracted — so the field was
deleted rather than pinned at `0.00s`. The "prints `0.00s` rather than
disappearing" rule is about one build's semantic-on vs semantic-off runs, not a
promise that the field set never changes across releases.)

| Dominant phase | What it means | Which issue addresses it |
|---|---|---|
| `download` | archive I/O bound | — (the decompression it used to be paired with is now fused into `parse`, #106) |
| `parse` | GIL-bound tree-sitter extraction — **plus, since #106, the archive's gzip decompression, tar-stream read and UTF-8 decode**, which used to be the separate `extract=` field | #108 (process-pool extraction) addresses the tree-sitter half only |
| `embed` | serial AI Gateway round trips | #107 (concurrent embedding) |
| `db` | per-file round trips | #105 (batched writes) |
| any of the above, on **unchanged** content | redundant work | #104 (file-level delta indexing) |

`#104` narrows the **db** and **embed** costs to a branch's actual delta, not
its size — but it does NOT touch `parse`: extraction still runs on every
file every run (tree-sitter must produce a `FileExtraction` before
`index_repo` can classify it), so an all-unchanged branch on a large repo
still pays its full `download`+`parse` cost. See `indexer.store`'s
`delta write set …` line (below) to tell "this branch is genuinely mostly-new"
from "this branch is mostly-unchanged but still parsing everything" — the
latter is exactly the case `#108` (process-pool extraction) or a future
extraction-skip step would address next.

Four fields need interpretation before you act on them:

- **`resolve=0.00s` on a default branch is expected, not a bug.** That branch's
  HEAD SHA came from the repo-level resolve, which happens once per repo outside
  every branch's total and is reported on the repo's `finished` line as
  `resolve=`. The `list=` on the same line is the branch-listing API call, which
  is `0.00s` unless the repo has `branches:` globs configured (it is not called
  at all otherwise) and which is paginated — on a monorepo with hundreds of
  branches it is a real, and otherwise invisible, cost. The elapsed value in
  `finished … in Xs` is measured on its own clock and is deliberately not
  reconciled against the parenthesised numbers.
- **`other=` is the unattributed residual**, `total` minus every measured phase,
  clamped at zero. It covers the temp-dir teardown plus the pre-flight disk
  check. Since #106 that teardown is an `rm -rf` of one compressed tarball, not
  of a freshly extracted multi-GB tree, so `other` shrinks materially on large
  repos — if you are reading an old run's numbers, do not go hunting for a
  teardown cost that no longer exists. It exists so the line has no silently
  missing time; a large `other` means something real is happening outside every
  instrumented phase and is worth chasing.
- **`embed=` covers chunking as well as the network.** It spans `iter_chunks`
  (CPU/GIL-bound) *and* the serial AI Gateway round trips. #107 addresses only
  the round trips, so before routing work there, confirm the phase is
  network-bound rather than chunking-bound (a follow-up may split it into
  `chunk=`/`embed=`).
- **`db=` excludes parse and sweep, but the walk still happens inside the
  transaction.** Files stream lazily through `index_repo`'s open transaction for
  bounded memory, so file production is timed separately and subtracted from
  `db`; the sweep is subtracted too. On the **non-semantic** path, though, the
  file walk itself materializes inside that open transaction — since #106 that
  is the tar stream (`ingest.py`), not `parse.py`'s `rglob`, on the first item.
  That is long-standing behavior which this instrumentation merely makes visible
  for the first time — it is not a new regression. What #106 *did* move into that
  window is **archive validation**: the decompression-bomb cap, the
  exactly-one-top-level-dir check, member-name and link-target safety, and any
  `tarfile` corruption error are now raised as the stream is consumed rather than
  before the connection is taken. A malformed archive therefore surfaces as a
  rolled-back transaction and one briefly-held pooled connection instead of a
  pre-connection failure. **The branch-level outcome is unchanged** —
  `failed`, exit code non-zero, nothing written. On the semantic (production)
  path nothing moved at all: the file list is materialized up front, so the
  archive is fully validated before any connection is opened.

### 2.2 The delta write set line (#104)

Every `index_repo` call also emits one `indexer.store` INFO line, immediately
before the sweep, in **both** the gate-open and gate-closed cases — one format
string, no conditional fields, so it stays greppable either way:

```
INFO indexer.store [acme/widgets]: acme/widgets@main: delta write set 412/30214 files (unchanged=29790 membership=12, semantics gate open)
INFO indexer.store [acme/gadgets]: acme/gadgets@main: delta write set 812/812 files (unchanged=0 membership=0, semantics gate closed: stored v3 != v4)
```

`unchanged` files write nothing at all (no file upsert, no symbol/edge
delete-reinsert, no chunk write) and are never re-embedded. `membership` files
are already stored under another branch and only need their `branches` array
unioned in, plus a chunk write if semantic is on (see §4's accepted
regressions). The leading fraction is `(changed/new) / (total seen)`. The gate
is per-BRANCH: it opens only once that branch's own `repo_branches` stamp is
at the current `INDEX_SEMANTICS_VERSION` — a branch's first run, or any run
after a semantics bump, always shows `semantics gate closed`.

```
grep 'delta write set' run.log         # one line per index_repo call
```

A branch stuck at a low `unchanged=` fraction run after run either genuinely
churns every run (nothing to fix) or has drifted out of delta eligibility —
check its `repo_branches.index_semantics_version` against the current
`INDEX_SEMANTICS_VERSION` and whether a sibling branch is stale (§4's
provenance gate).

---

## 3. The three limits, and why raising concurrency is a bad trade

| Limit | Value | What it bounds |
|---|---|---|
| `index_concurrency` | 1..8, default **4** | Repos in flight |
| `MAX_TARBALL_BYTES` | 500 MB | The compressed download, per worker |
| `MAX_EXTRACTED_BYTES` | 2 GB | The streamed uncompressed content, per branch |

Only the first of the two byte caps is a **disk** cap. Since #106 the tarball is
streamed once, in memory, and is never extracted, so `MAX_EXTRACTED_BYTES` is a
**work** cap — a decompression-bomb guard on how much content one branch may pull
out of its archive — and it lives in `indexer/ingest.py`, beside its only
consumer, rather than in `indexer/fetch.py`. The two therefore no longer sum:
the compressed tarball is the only artifact on disk, so peak local disk is
`index_concurrency` × 500 MB:

| `index_concurrency` | Peak local disk |
|---|---|
| 1 | 0.5 GB |
| 2 | 1 GB |
| **4 (default)** | **2 GB** |
| 8 (ceiling) | 4 GB |

**Returns at the ceiling are sublinear; the disk cost is not.** Symbol
extraction was measured at **0.95x on 4 threads** — the tree walk is
GIL-serialized and is ~56% of extraction time, so Amdahl's law caps the speedup
well below 8x. Meanwhile the 4 GB is a hard, linear, unavoidable cost. Raise
`index_concurrency` to 8 only knowing you are buying a fraction of a speedup
with a doubling of disk. (#106 lowered these numbers by 5x but deliberately did
**not** move the default of 4; re-deriving it is #109's job.)

**Semantic indexing clamps the pool to 2**, regardless of `index_concurrency`.
That clamp is a *memory* bound, not a CPU one: embedding materialises a whole
repo's chunks in memory (~0.5-0.8 GB per worker). The clamp is logged:

```
INFO indexer.job [-]: semantic enabled: clamping index_concurrency 6 -> 2 (memory bound: ...)
```

### The connection pool follows the workers

Each worker holds exactly one connection, so the engine is built with
`pool_size == effective workers`, `max_overflow=0`, `pool_timeout=30`. There is
deliberately **zero headroom**: a connection leak stalls loudly for 30 seconds
and then raises, rather than growing the pool silently. If you see a
`QueuePool limit ... connection timed out` from the indexer, suspect a leaked
connection, not an undersized pool — the pool is sized to the workers by
construction.

The app/serving pool is separate and unaffected (5, paired with a matching
`CapacityLimiter`).

### The disk guard

Before any bytes are downloaded, each worker checks free space on the filesystem
it is about to write to. Below 0.5 GB it fails **that repo**, not the run:

```
ERROR indexer.job [acme/leviathan]: failed to index acme/leviathan
OSError: insufficient local disk for acme/leviathan: 104214016 bytes free at /tmp/tmpXXXX,
need 500000000 (...); lower index_concurrency in config.yaml
```

A shortfall fails that repo alone, not the run, and the job exits non-zero.
**The fix is named in the error:** lower `index_concurrency` in `config.yaml`
and re-run — the completed repos are skipped, so the retry is cheap (see §1).

**This guard is a pre-flight sanity check, not admission control.** It reserves
nothing: each worker calls `shutil.disk_usage` independently before it writes,
so at 1 GB free with 4 workers all four pass their check and all four then
download. It reliably catches the *steady-state* case — disk already low when a
repo starts — and turns it into the legible error above. It does **not** bound
the *transient* case, where the combined footprint exhausts the disk mid-flight;
that still surfaces as an opaque `tarfile` error. Sizing `index_concurrency` to
your actual disk (0.5 GB per worker peak) is the real control.

---

## 4. Forcing a re-index

There is deliberately **no `--force_reindex` flag.** Forcing a re-index means
clearing the provenance stamp, after which the normal skip logic re-indexes the
affected repos on the next scheduled or manual run.

**The stamp the skip seam actually reads is `repo_branches`, not `repos`.**
`indexer/job.py`'s `_read_stamps` selects
`RepoBranch.last_indexed_commit, RepoBranch.index_semantics_version` — the
`repos` table's `index_semantics_version` column is a deprecated legacy stamp
that no decision anywhere reads (`app/db/models.py` documents it write-only).
An `UPDATE repos SET index_semantics_version = NULL` is therefore a **no-op**
against the skip seam: the branch will look untouched and re-index on its own
next scheduled cycle, not immediately, and the operator following an older
version of this runbook would see nothing happen.

```sql
-- everything
UPDATE repo_branches SET index_semantics_version = NULL;

-- one repo, every branch
UPDATE repo_branches SET index_semantics_version = NULL
  WHERE repo_id = (SELECT id FROM repos WHERE name = 'acme/widgets');

-- one repo, one branch
UPDATE repo_branches SET index_semantics_version = NULL
  WHERE repo_id = (SELECT id FROM repos WHERE name = 'acme/widgets')
    AND branch = 'main';
```

Then run the job (`make index TARGET=<target>` or `databricks bundle run
code_search_index -t <target>`).

### 4.1 File-level delta indexing (#104): what changes about this remedy

Once a branch's `repo_branches.index_semantics_version` matches the current
`INDEX_SEMANTICS_VERSION`, `index_repo` skips rewriting any file whose
`(path, content_sha)` it already has stored for that branch — see
`indexer.store`'s module docstring for the full classification and the
correctness proof. Two consequences change what "clear the stamp" actually
buys you:

**A degraded branch no longer self-heals on its own.** Before #104, ANY
re-index rewrote the whole branch, so a branch whose semantic precompute
failed (a chunk-cap breach, an embedder outage) caught its chunks up
automatically on the next successful run. Under delta indexing, only
*changed* files get re-embedded — a branch that never changes again carries
that gap **forever** unless you clear its stamp. `indexer.job` emits one
run-completion WARNING naming every branch that finished this way:

```
WARNING indexer.job [-]: 2 branch(es) finished with degraded semantic coverage this run (chunk precompute failed; core index is current, chunks are not, and delta indexing will NOT catch them up on their own -- clear their repo_branches.index_semantics_version stamp to force a full re-embed, see docs/runbooks/indexing-parallelism.md §4): acme/big-repo@main, acme/other@release
```

Grep for it (`grep 'degraded semantic coverage' run.log`) and clear the named
branches' stamps with the one-branch form above once the underlying cause
(chunk cap, embedder outage) is resolved.

**The provenance gate can force a full re-index you did not ask for.** A
branch only takes the cheaper "membership-only" path (acquiring content a
*sibling* branch already stored, e.g. two branches sharing most of a
monorepo) when **every** `repo_branches` row for that repo is at the current
semantics version. If you clear one branch's stamp and leave siblings
untouched, that is fine — but a repo with one branch stuck at an old version
for any other reason (a persistently failing branch) will force every OTHER
branch of that repo through the full write path for any file it shares with
the stuck one, even though those branches are otherwise fully caught up. The
`delta write set …` line's `membership=` count going to zero across a whole
repo, with `unchanged=` still high, is the symptom — check for a sibling
branch stuck at a stale `index_semantics_version` before assuming something
is broken.

**`semantic_max_chunks_per_repo` is enforced per RUN, not per branch's whole
corpus.** The cap is evaluated over whatever `_precompute_chunk_writer`
embeds, which under delta indexing is only the changed/new/membership-only
files. A branch can drift above the nominal cap between full reindexes (a
semantics bump, or its first index) — re-enforced in full at each of those.
Not a bug; see `app/config.py`'s `semantic_max_chunks_per_repo` comment.

### Who can run this — read before you need it

`UPDATE` on `repo_branches` (and `repos`) is held by **the identity that deployed
the schema**, which owns every table, `repo_branches` included. Concretely:

- **dev:** the developer who ran `make migrate` / `scripts/deploy.sh`. Table
  ownership carries `UPDATE` implicitly; no explicit grant was ever issued for
  it (`scripts/deploy.sh:118`).
- **prod:** whichever identity ran the prod deploy — typically the CI/deploy
  service principal, not a human.
- The **job run-as SP** (`JOB_RUN_AS_SP`) also holds `UPDATE` on all tables in
  the schema via `build_job_grants`, so it can run the statement too.
- The **app SP** is read-only and **cannot**.

**Residual risk, stated plainly:** the operator recovering at 2am may not be the
identity that deployed. If you connect as yourself and the `UPDATE` fails on
permissions, that is the expected failure, not a bug. You need either the
deploying identity's credentials or the job SP's. Find out *now* which identity
deployed your prod target and record it somewhere you can reach at 2am; there is
no in-repo record of it.

---

## 5. Changing extraction semantics

If you change **what** gets extracted — `indexer/symbols.py`,
`indexer/parse.py`, `indexer/languages.py` — you **must** bump
`INDEX_SEMANTICS_VERSION` in `app/db/models.py`.

**The same obligation now extends past the tripwire's watched files (#104).**
`indexer/parse.py`'s chunker is already a watched path, so a change to
`iter_chunks` still fires the tripwire. Swapping the embedding MODEL
(`app/embed.py`) or changing `SEMANTIC_EMBEDDING_DIM` (`app/config.py`)
without bumping `INDEX_SEMANTICS_VERSION` is NOT caught by the tripwire (both
are deliberately unwatched — `app/embed.py` would otherwise fire on
unrelated retry/batching edits and a noisy tripwire gets disabled) and now
leaves every UNCHANGED file's vectors permanently stale under file-level
delta indexing — before #104 the next HEAD move re-embedded everything
anyway, so a missed bump here was self-limiting; it no longer is. This is not
a new pattern: `INDEX_SEMANTICS_VERSION` version `2` was minted for exactly
this reason (turning semantic search on by default so `chunks` backfills).
Treat this as a reviewed convention, the same posture `indexer.store`'s
module docstring takes for `lang`/`size` re-derivation.

Without a bump, every already-indexed repo keeps serving output from the *old*
extractor and never re-indexes, because its stored stamp still matches HEAD.
The failure is silent and open-ended: the index looks perfectly current.

This obligation is enforced, not merely documented.
`tests/unit/test_semantics_version_tripwire.py` diffs the branch against its
base and fails if a semantics module changed without the constant changing. It
skips (rather than fails) when git or the base ref is unavailable, so shallow
clones and detached checkouts do not fail spuriously.

Bumping the constant re-indexes every repo on the next run. That is the intended
cost of the change — budget the run time for it.

### The `0002` migration: two rollout paths

`0002` adds `index_semantics_version` and backfills it. Two options:

1. **Filtered backfill (recommended, and what `0002` does):** stamp only rows
   indexed in the last 48 hours. Recently-indexed repos are trusted as current;
   everything older re-indexes on the next run. This bounds the post-migration
   re-index to the stale tail instead of the whole corpus.
2. **Unconditional NULL:** leave every row unstamped, forcing a full re-index of
   every repo on the next run. Correct but expensive; choose it only if you have
   reason to distrust the recent rows.

---

## 6. Deploy coupling — this change is NOT schema-only

The job role's grants now include `SELECT` (`app/db/grants.py`), because the
job's pre-fan-out stamp read issues a plain `SELECT` against `repos`.

**An existing deployment therefore needs the grants re-applied, not just a
schema migrate.** Re-run the grant step:

```
APP_SP_ROLE=<app sp client id> JOB_WRITER_ROLE=<job run-as sp client id> \
  make migrate TARGET=<target> ARGS=--apply-grants
```

or simply re-run `scripts/deploy.sh` for the target, which performs the
post-activation grant step (`[6/8]`) as part of the normal flow. Grants and
`upgrade head` are both idempotent, so re-running is safe.

**Caveat, stated honestly:** the job's existing `RETURNING` clauses and filtered
`DELETE` statements already require `SELECT`, so in practice the privilege
likely already reaches the job SP by some route this repo does not record
(ownership, a role default, or a prior manual grant). The addition is
belt-and-braces — but the new explicit `SELECT` query makes the dependency
first-order rather than incidental, and **it has not been verified against a
real deployment.** If the job fails on `permission denied for table repos`,
this is the step you skipped.
