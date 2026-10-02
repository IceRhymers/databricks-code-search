# Runbook: Lakebase Search operations

Operational guidance for the Lakebase Search surface this project depends on — the
`lakebase_vector` / `lakebase_text` / `lakebase_tokenizer` extensions and the
`lakebase_ann` / `lakebase_bm25` indexes migration `0004` builds on `chunks`. This
material became documented at Lakebase Search GA and did not exist in the beta docs;
none of it is required for day-to-day operation, but it is the reference for upgrades,
index maintenance, and cold-start latency. Source: the Databricks Lakebase Search
docs (<https://docs.databricks.com/aws/en/oltp/projects/lakebase-search> and the
per-extension pages linked from it).

For *enabling* Lakebase Search on a project (the deploy-time prerequisite), see
[`semantic-enablement.md`](semantic-enablement.md) §1 — this runbook covers what
happens after it is enabled.

## 1. What ships where

| Object | Created by | Notes |
|---|---|---|
| `lakebase_tokenizer`, `lakebase_vector`, `lakebase_text` extensions | `0004` (`CREATE EXTENSION ... CASCADE`, tokenizer → vector → text) | Database-wide; never dropped by downgrades |
| `chunks.embedding vector(1024)` + `ix_chunks_embedding_ann` (`lakebase_ann`, `vector_cosine_ops`) | `0004` | ANN leg of hybrid search |
| `chunks.ts tsvector` (generated) + `ix_chunks_ts_bm25` (`lakebase_bm25`) | `0004` | BM25 leg of hybrid search |

## 2. Upgrading the extensions and index storage format

Lakebase Search updates ride Lakebase updates, but **two things upgrade separately and
only on demand** (per the GA docs):

- **Extension version** — the SQL objects. Check and bump:
  ```sql
  SELECT name, installed_version, default_version
    FROM pg_available_extensions
   WHERE name IN ('lakebase_vector', 'lakebase_text', 'lakebase_tokenizer');
  ALTER EXTENSION lakebase_vector UPDATE;   -- repeat per extension, when default_version is newer
  ```
- **Index storage format** — the on-disk layout of `lakebase_ann`/`lakebase_bm25`
  indexes. New indexes pick up the latest format automatically; existing indexes are
  upgraded by rebuilding them. Find indexes on an older format (the docs' query):
  ```sql
  SELECT oid::regclass AS index,
         lakebase_ann_index_info(oid::regclass)::json ->> 'version' AS storage_format_version
    FROM pg_class
   WHERE relam = (SELECT oid FROM pg_am WHERE amname = 'lakebase_ann')
     AND relkind = 'i';
  ```
  Upgrade with `REINDEX INDEX CONCURRENTLY ix_chunks_embedding_ann;` — reads and writes
  continue, it just takes longer. Upgrading is not urgent (old formats stay compatible);
  do it when convenient rather than deferring indefinitely.

## 3. BM25 statistics and VACUUM

`lakebase_bm25` computes corpus-wide statistics at **index build time** and refreshes
them on **VACUUM**. The `chunks` write path is delete-and-reinsert on every index run,
so statistics drift between vacuums is expected and benign for ranking at our scale;
after an unusually large re-index (many repos re-embedded at once, e.g. an
`INDEX_SEMANTICS_VERSION` bump), a manual refresh is cheap:

```sql
VACUUM chunks;
```

For a search-dedicated table the GA docs recommend pinning the insert-triggered
autovacuum threshold so it does not grow with table size — migration `0006` applies
exactly that to `chunks`:

```sql
ALTER TABLE chunks SET (autovacuum_vacuum_insert_scale_factor = 0);
```

**MVCC caveat (GA docs, worth knowing at 2am):** BM25 global statistics are not
MVCC-versioned. If VACUUM (including autovacuum) updates statistics while a transaction
holds an older snapshot, scores and top-K results can shift *within* a
`REPEATABLE READ` transaction. Row visibility stays MVCC-correct; rankings are not
snapshot-stable. `semantic_search` runs under a per-request transaction — do not rely
on byte-identical BM25 orderings across concurrent vacuums.

**Result cap:** `lakebase_bm25.default_limit` (session GUC, default `1000`) caps how
many rows the index returns. The BM25 leg in `app/search/semantic.py` retrieves
top-200 (`SEMANTIC_TOP_K`), comfortably under the cap — no action, noted so a future
top-K increase checks the cap first.

## 4. Cold-start prewarm (scale-to-zero)

`resources/lakebase.yml` pins `suspend_timeout_duration: 300s`, so the endpoint scales
to zero routinely. `lakebase_ann` indexes are storage-backed and survive scale-to-zero
without warmup, but the first ANN queries after a resume pay cold-cache latency. If
semantic queries feel slow right after an idle period, prewarm the hot structures:

```sql
-- Full hot portion used for search (default scope):
SELECT lakebase_ann_prewarm('ix_chunks_embedding_ann');
-- Cheaper: routing structures only (better cost/latency tradeoff on large indexes):
SELECT lakebase_ann_prewarm('ix_chunks_embedding_ann', scope => 'routing');
```

This is a manual lever, not something the app runs — steady-state latency after the
first few queries is unaffected.

## 5. Query-time tuning knobs (defaults are fine; documented so they are findable)

`lakebase_ann` session GUCs: `lakebase_ann.probes` (IVF partitions scanned; shape must
match the index's `lists` from `lakebase_ann_index_info`), `lakebase_ann.epsilon`
(full-precision rerank budget), `lakebase_ann.prefilter` (evaluate non-vector filters
before reranking). `lakebase_bm25`: `default_limit` (above), `prefilter`, `enable_scan`
(`off` forces a seq scan — a testing escape hatch), and index storage params `k1` /
`b`. All are untuned in this project — there has been no recall complaint to justify
changing them; measure before touching.

## Reference

- Lakebase Search overview: <https://docs.databricks.com/aws/en/oltp/projects/lakebase-search>
- `lakebase_vector` (ANN): <https://docs.databricks.com/aws/en/oltp/projects/lakebase-vector>
- `lakebase_text` (BM25): <https://docs.databricks.com/aws/en/oltp/projects/lakebase-text>
- `lakebase_tokenizer`: <https://docs.databricks.com/aws/en/oltp/projects/lakebase-tokenizer>
- Sibling runbooks: [`semantic-enablement.md`](semantic-enablement.md) (enablement,
  embeddings, opt-out), [`ci-lakebase.md`](ci-lakebase.md) (integration gate),
  [`indexing-parallelism.md`](indexing-parallelism.md) (the write path VACUUM sees).
