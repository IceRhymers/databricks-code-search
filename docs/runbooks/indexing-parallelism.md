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
INFO indexer.job [-]: local disk at /tmp: 41.2 GB free of 64.0 GB total; 4 worker(s) x 2.5 GB peak
INFO indexer.fetch [acme/widgets]: ...
INFO indexer.job [acme/widgets]: finished acme/widgets in 71.30s
INFO indexer.job [acme/gadgets]: skipped acme/gadgets: already indexed at abc123 (semantics v1) in 0.41s
```

**To find the giant:** grep for `finished .* in` and sort by the elapsed number.
Wall-clock for the whole run is bounded below by the single slowest repo, so if
one repo dominates, raising `index_concurrency` will not help — that is Amdahl's
law asserting itself at the repo level, and the fix is to exclude the repo or
accept the duration.

**To decide whether tuning is worth it:** compare the total on the completion
line against the sum of the per-repo elapsed times. If the total is already
close to the slowest single repo, the pool is not the bottleneck.

---

## 3. The three limits, and why raising concurrency is a bad trade

| Limit | Value | What it bounds |
|---|---|---|
| `index_concurrency` | 1..8, default **4** | Repos in flight |
| `MAX_TARBALL_BYTES` | 500 MB | The compressed download, per worker |
| `MAX_EXTRACTED_BYTES` | 2 GB | The uncompressed tree, per worker |

The two byte caps **sum**, they do not `max()`: the tarball stays on disk inside
the worker's temp directory while the extraction grows beside it. Peak local
disk is therefore:

| `index_concurrency` | Peak local disk |
|---|---|
| 1 | 2.5 GB |
| 2 | 5 GB |
| **4 (default)** | **10 GB** |
| 8 (ceiling) | 20 GB |

**Returns at the ceiling are sublinear; the disk cost is not.** Symbol
extraction was measured at **0.95x on 4 threads** — the tree walk is
GIL-serialized and is ~56% of extraction time, so Amdahl's law caps the speedup
well below 8x. Meanwhile the 20 GB is a hard, linear, unavoidable cost. Raise
`index_concurrency` to 8 only knowing you are buying a fraction of a speedup
with a doubling of disk.

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
it is about to write to. Below 2.5 GB it fails **that repo**, not the run:

```
ERROR indexer.job [acme/leviathan]: failed to index acme/leviathan
OSError: insufficient local disk for acme/leviathan: 1904214016 bytes free at /tmp/tmpXXXX,
need 2500000000 (...); lower index_concurrency in config.yaml
```

A shortfall fails that repo alone, not the run, and the job exits non-zero.
**The fix is named in the error:** lower `index_concurrency` in `config.yaml`
and re-run — the completed repos are skipped, so the retry is cheap (see §1).

**This guard is a pre-flight sanity check, not admission control.** It reserves
nothing: each worker calls `shutil.disk_usage` independently before it writes,
so at 5 GB free with 4 workers all four pass their check and all four then
download. It reliably catches the *steady-state* case — disk already low when a
repo starts — and turns it into the legible error above. It does **not** bound
the *transient* case, where the combined footprint exhausts the disk mid-flight;
that still surfaces as an opaque `tarfile` error. Sizing `index_concurrency` to
your actual disk (2.5 GB per worker peak) is the real control.

---

## 4. Forcing a re-index

There is deliberately **no `--force_reindex` flag.** Forcing a re-index means
clearing the provenance stamp, after which the normal skip logic re-indexes the
affected repos on the next scheduled or manual run.

```sql
-- everything
UPDATE repos SET index_semantics_version = NULL;

-- one repo
UPDATE repos SET index_semantics_version = NULL WHERE name = 'acme/widgets';
```

Then run the job (`make index TARGET=<target>` or `databricks bundle run
code_search_index -t <target>`).

### Who can run this — read before you need it

`UPDATE` on `repos` is held by **the identity that deployed the schema**, which
owns the tables. Concretely:

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
