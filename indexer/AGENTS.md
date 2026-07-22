<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# indexer

## Purpose
The serverless indexing job (`code-search-index`, wired as a `python_wheel_task` entry point in `pyproject.toml`). It reads the central `config.yaml` from the Databricks workspace, resolves it into a deduped list of GitHub repos (fail-fast on empty or oversized results, before any tarball is fetched or database connection opened), then fans repos out over a bounded thread pool. Per repo it resolves the default branch's immutable HEAD SHA, resolves configured branch globs into a concrete branch list, and — sequentially per branch — downloads the tarball by SHA over plain HTTPS (no git binary), extracts it safely, parses text files, extracts tree-sitter symbols, and writes everything in one atomic per-(repo, branch) transaction with content-SHA-deduped storage and a mark-and-sweep of stale branch membership. When semantic search is enabled, files are also chunked and embedded via `app.embed` — outside the transaction — and precomputed vectors are written through a `chunk_writer` seam. The process exits non-zero if any branch fails.

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Empty package marker. |
| `branches.py` | Resolves a connection's branch globs against a repo's real branch list via `fnmatchcase`, returning a typed `BranchResolution` (`branches`, `complete`, `dropped`, `cap`). Default branch always included and always first; empty globs means default-branch-only with no GitHub branches API call, always `complete=True`. `SOFT_BRANCH_CAP = 20` truncates (loud warning naming the cap and dropped branches, not a failure; sets `complete=False`, blocking reconciliation for that repo — #61); no override flag — fix the config. |
| `chunk_store.py` | `write_chunks`: delete-and-reinsert one file's rows in the `chunks` table (no natural key). Takes a live `Connection`; never opens an engine, never calls the embedder — vectors arrive precomputed. No `repo_id` parameter: `chunks` is scoped by `file_id` only. |
| `fetch.py` | All GitHub HTTP: paginated org/user repo enumeration (`RepoMeta`), branch listing, `resolve_ref`/`resolve_branch_head` (branch name -> immutable SHA), streamed tarball download capped at `MAX_TARBALL_BYTES` (500 MB), safe extraction (`filter="data"`, bomb check) capped at `MAX_EXTRACTED_BYTES` (2 GB), `assert_disk_headroom` (2.5 GB per worker, both caps alive at once). `RateLimitError` is deliberately narrow: 429 always; 403 only with `Retry-After` or `X-RateLimit-Remaining: 0` — other 403s are permission failures. |
| `hashing.py` | `content_sha`: canonical SHA-256 hex of content (`None` -> empty string). Single source of truth for `files.content_sha`; must stay byte-identical to the `0003` migration's SQL backfill forever or cross-branch dedup silently breaks (`tests/integration/test_content_sha_parity.py` is the gate). |
| `job.py` | Entry point and orchestration: `main()`/`run()`, `ThreadPoolExecutor` sized by `effective_workers` (unit of work = one repo, all branches sequential), batched `repo_branches` stamp read, per-branch skip-if-unchanged, per-branch `BranchOutcome` classification (`indexed`/`skipped`/`conflict`/`failed`), semantic precompute (`_precompute_chunk_writer`), `ContextVar`-based `[repo]` log attribution. Returns 1 if any branch failed; conflicts self-heal and do not fail the run. |
| `languages.py` | Single source of truth shared by `parse.py` and `symbols.py`: `EXT_TO_LANG` (8 extensions -> tree-sitter language names), `SYMBOL_KINDS` (node type -> symbol kind per language), `MAX_FILE_BYTES` (1 MB), `SEMANTIC_CHUNK_MAX_CHARS` (2000, ~4 chars/token), and the frozen dataclasses `ParsedFile`, `Chunk`, `ExtractedSymbol`, `IndexCounts`. |
| `parse.py` | `iter_source_files`: walk an extracted tree, yield every text file (unknown extensions kept with `lang=None`, since grep runs over all files); skips `.git/`, symlinks, files > `MAX_FILE_BYTES` (stat before read), NUL-sniffed binaries, and UTF-8 decode failures; strips surviving NULs (Postgres `text` rejects them). `iter_chunks`: deterministic line-aligned chunking, no overlap, no mid-line splits, 1-based inclusive line ranges. |
| `repo_config.py` | Pydantic schema for `config.yaml` (`RepoConfig` / `GitHubConnection` / `ExcludeRules`), `normalize_repo` (URL/SSH/bare -> canonical `org/repo`, GitHub hosts only), `parse_config` (pure) vs `read_workspace_config` (SDK I/O, wraps every failure in `ConfigError` with HTTP status so 404 "never synced" stays distinguishable from 403 "no permission"), and `effective_workers` (the semantic clamp). A connection with no `orgs`/`users`/`repos` fails validation. `index_concurrency`: 1-8, default 4 — a disk bound (2.5 GB peak per worker), not a CPU one. Deliberately import-light: pydantic + PyYAML + stdlib only. |
| `resolve.py` | `resolve_repos`: enumerate org/user selectors, apply `ExcludeRules` to enumerated repos only (explicit `repos` entries always win, unfiltered), dedup case-insensitively keeping first-seen spelling, union each connection's `branches:` globs into per-repo `RepoEntry.branch_globs`, then fail fast: `EmptyConfigError` on zero repos (indexing nothing must not exit 0), `RepoCeilingError` above `MAX_REPOS` (500, overridable via `--max_repos`). Enumerator params are a test seam only, not provider dispatch. |
| `store.py` | `index_repo`: the single atomic unit of work for one (repo, branch) inside `with conn.begin():` — repos upsert, `repo_branches` CAS-baseline read, per-file array-union upsert on `(repo_id, path, content_sha)` with branch-membership union, delete-and-reinsert symbols, optional `chunk_writer` call, membership sweep (strip this branch from unseen rows, delete rows with empty `branches`; skipped with a WARNING on an empty seen-set), then CAS stamp (raises `StaleIndexError` on mismatch, rolling everything back). Pure DML, no TEMP tables (job role has no TEMP privilege on Lakebase). |
| `symbols.py` | `extract_symbols`: tree-sitter parse via `tree_sitter_language_pack`, full-tree walk (nested definitions captured), named nodes only, kinds from `SYMBOL_KINDS`, 1-based lines. Parser cache is per-thread (`threading.local`) as insurance against a future GIL-releasing `parse()`. |

## For AI Agents

### Working In This Directory
- **Immutable-SHA indexing**: tarballs are always downloaded by the resolved HEAD SHA, never a branch name — a push between resolve and download would otherwise desync the tree from the `head_sha` stamped into `files.commit` and corrupt the mark-and-sweep key. Keep `resolve_branch_head` -> `download_tarball` in that order.
- **Fail-fast before any fetch**: config load, enumeration, exclude filtering, dedup, and the repo ceiling all complete in `resolve_repos` before the first tarball download or database connection. A bad config exits 1 having touched nothing.
- **Transaction grain is one (repo, branch)**: `index_repo` owns exactly one `conn.begin()`. Branches within a repo run sequentially in one worker — that single-writer-per-repo property is what makes the sweep's plain UPDATE/DELETE sound without an advisory lock, and what `StaleIndexError` exists to loudly assert if it's ever removed. Never parallelize branches within a repo without revisiting `store.py`.
- **One logical corpus writer, at the run level**: `resources/job.yml` pins `max_concurrent_runs: 1` (queueing retained), so at most one run of this job is ever active — the invariant global desired-state reconciliation (#56) depends on. This is separate from (and does not replace) the per-branch sequencing above: it bounds concurrent job RUNS, not per-run concurrency, and it does NOT cover a writer outside this job (a second job, a manual `bundle run`, or future per-repo/per-branch task sharding within one run) — any of those needs a shared database fencing/lease protocol first. See `docs/runbooks/indexing-parallelism.md` §1.1 for the full invariant and its coverage boundary.
- **`content_sha` parity is forever**: `hashing.content_sha` must stay byte-identical to the `0003` migration's SQL expression, or content dedup silently mints duplicate rows.
- **No network inside the transaction**: embeddings are precomputed in `job._precompute_chunk_writer` before `engine.connect()`; `chunk_store.write_chunks` is pure DML. The semantic layer is additive — every semantic failure (embedder down, chunk ceiling, count mismatch) degrades to indexing the core corpus without chunks, never to losing the branch.
- **Redaction**: the GitHub token is read from Databricks secrets (base64), lives only in the injected `httpx.Client`'s `Authorization` header, and is never logged; request headers are never logged; the job never lowers root/SDK/httpx log levels (`tests/unit/test_job_redaction.py` enforces this).
- **Test seams are injected callables**, not registries: `run()` accepts `workspace_client`, `http_client`, `engine`, `index_fn`, `embed_fn`, `config_loader`; `resolve_repos` accepts enumerators; `store.index_repo` and `chunk_store.write_chunks` take a live `Connection`. `resolve_repos` itself is deliberately NOT injectable in `run()` — tests drive it through `httpx.MockTransport`.
- **The schema is provider-extensible; the code is GitHub-only.** No `type`-keyed dispatch anywhere — adding GitLab means a new union member plus a new fetch implementation, not a provider abstraction.
- **Import discipline**: `repo_config.py` stays import-light (no httpx/SQLAlchemy/SDK at module level); `normalize_repo` is imported from `repo_config`, never from `job` (import cycle); `app.embed`'s databricks-sdk dependency is only imported when semantic is on.

### Testing Requirements
- `make test` (`pytest -m "unit or observability"`, no external deps): `tests/unit/test_branches.py`, `test_chunk_store.py`, `test_chunking.py`, `test_fetch.py`, `test_job.py`, `test_job_redaction.py`, `test_languages.py`, `test_parse.py`, `test_repo_config.py`, `test_resolve.py`, `test_symbols.py`, `test_store_chunk_writer.py`, `test_semantics_version_tripwire.py`.
- `make test-integration` (needs Postgres): `tests/integration/test_store.py`, `test_store_chunk_writer.py`, and `test_content_sha_parity.py` (the hard gate on `hashing.py` vs the migration backfill).
- Schema tests must stay fast: changes to `repo_config.py` must not add heavy imports.

### Common Patterns
- Frozen dataclasses as data carriers (`ParsedFile`, `Chunk`, `ExtractedSymbol`, `IndexCounts`, `RepoMeta`, `RepoEntry`, `BranchOutcome`); shared vocabulary lives in `languages.py` so `parse` and `symbols` can never disagree.
- `pg_insert(...).on_conflict_do_update(...).returning(...)` with a no-op `SET` on conflict — `DO NOTHING ... RETURNING` returns no row on conflict and would break the id bootstrap.
- Delete-and-reinsert for child rows with no natural key (symbols, chunks).
- `fnmatchcase`, never `fnmatch`: plain globs must behave identically on every platform.
- Guardrail constants with config-level fixes, not override flags: `SOFT_BRANCH_CAP` (20), `MAX_REPOS` (500, the one exception — `--max_repos`), `MAX_FILE_BYTES`, `MAX_TARBALL_BYTES`, `MAX_EXTRACTED_BYTES`.
- Errors name the fix: exceptions carry the config key or job parameter to change (`exclude.size_mb`, `index_concurrency`, `--max_repos`).
- Broad `except Exception` at classification boundaries only (`run()`'s config/resolve handlers, `_index_one_branch`), each with a comment explaining why broad is load-bearing.
- Lazy generators through the open transaction for bounded memory (semantic mode is the exception — it materializes the file list because embedding needs all chunk texts up front).

## Dependencies

### Internal
- `app.config` (`Settings`, `get_settings` — `semantic_enabled`, `semantic_max_chunks_per_repo`)
- `app.db.client` (`create_db_engine` — pool sized to the worker count, `max_overflow=0`)
- `app.db.models` (`Repo`, `RepoBranch`, `File`, `Symbol`, `INDEX_SEMANTICS_VERSION`)
- `app.db.semantic` (`chunks` table, from `chunk_store.py`)
- `app.embed` (`get_embedder`, `EmbedFn`, `EmbeddingCountMismatchError` — the job embeds through `app.embed`, lazily, only when semantic is enabled)

### External
- `httpx` (all GitHub HTTP; token header set by the caller)
- `sqlalchemy` (Core + postgresql `insert` upserts; connections injected, engines owned only by `job.run`)
- `pydantic` + `pyyaml` (config schema and parsing)
- `tree-sitter` / `tree-sitter-language-pack` (symbol extraction)
- `databricks-sdk` (workspace config read + secrets, injected/lazy — never imported at module level here)
- stdlib: `tarfile` (extraction with `filter="data"`), `concurrent.futures`, `tempfile`, `shutil`, `fnmatch`, `hashlib`, `argparse`, `contextvars`

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
