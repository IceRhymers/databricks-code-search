# Runbook: reference-edge schema (0005)

What an operator needs to know about the `reference_edges` table added in migration
`0005`: what it stores (and doesn't yet), why it's grant-coupled like `repo_branches`
(`0003`) and `chunks` (`0004`) before it, and how to verify the app/job grants actually
landed on a given deploy target.

---

## 1. What this table is

`reference_edges` is a **raw, unresolved** call/import edge extracted from one file's
content-version: `(edge_kind, target_name, line, enclosing_*)` per site tree-sitter finds,
with `edge_kind IN ('call', 'import')` enforced by a CHECK constraint. It is part of the
knowledge-graph epic (#82): `target_name` is resolved to concrete `symbols` rows at
**query time**, by name-join — shipped in #86 (see §4) — this table deliberately carries
**no foreign key to `symbols`**. Symbol ids churn on every per-file delete-and-reinsert, and
an FK would couple the two rewrite orders inside the indexing transaction for no query
benefit.

The enclosing symbol (the function/class a call or import site sits inside, if any) is
denormalized onto the row as `enclosing_name` / `enclosing_kind` / `enclosing_start_line`
/ `enclosing_end_line`, all nullable — `NULL` means module/top-level scope, exactly the
same convention `symbols` and `chunks` use elsewhere. There is no `branches` column and no
`commit` column: branch membership rides `files.branches` at query time (the resolver
joins through `files` with the same `coalesce(default_branch,'HEAD')` conjunct used
everywhere else), and `files.commit` is documented-ambiguous under multi-branch dedup and
must gain no new readers.

**#84 shipped the writer.** `indexer/symbols.py`'s `extract_file` walks each file's parse
tree once, emitting both `symbols` and `reference_edges` candidates from the same pass
(`indexer/languages.py`'s `EDGE_NODE_KINDS`, Python-only for now — the other six languages
land in #85). `indexer/store.py::index_repo` writes them exactly like `symbols`: an
unconditional per-file `DELETE` followed by a bulk reinsert, inside the same transaction as
the rest of that file's row, so a file whose edges all vanish still sheds its stale rows.
`indexer/store.py`'s cascade-owning functions (`index_repo`'s membership sweep,
`reconcile_retired_branches`, `reconcile_removed_repos`) already enumerate
`reference_edges` alongside `symbols`/`chunks` in their docstrings and rely on the same
FK-cascade mechanism proven in §7.2 of the design doc and in
`tests/integration/test_reconcile.py` / `test_store.py` — no behavior change was needed to
make the cascade correct, because both `repos -> reference_edges` and
`files -> reference_edges` are `ON DELETE CASCADE` foreign keys.

**What gets extracted (Python, #84):**

- **`call`** edges target the rightmost identifier of the callee: `f()` / `a.b.f()` /
  `self.f()` all target `f`. Callees with no rightmost identifier (`xs[0]()`, the outer call
  of `f()()`) are skipped — candidate-set semantics, not full resolution.
- **`import`** edges target the full dotted path as written, alias-insensitive:
  `import a.b.c as d` targets `a.b.c`, not `d`. `from a.b import c, d as e` yields two edges
  (`a.b.c`, `a.b.d`). Relative imports preserve source fidelity (`from . import x` ->
  `.x`; `from ..p import q` -> `..p.q`). A wildcard `from a.b import *` yields one edge for
  the module itself (`a.b`).
- **Enclosing attribution** is the innermost *named* definition on the walk stack when the
  call/import node is visited (`None` = module/top-level scope) — a call in a class body
  outside any method attributes to the class, not to `None`.
- Duplicate sites (the same target called twice on one line) are two rows by design; there
  is no uniqueness constraint, and the query-time resolver (#86) ranks candidates.

**Operational consequence of the `INDEX_SEMANTICS_VERSION` bump (2 -> 3, #84):** every
already-indexed branch's stored `(head_sha, index_semantics_version)` stamp now mismatches
the running code's version, so the *next* run of every branch is a full re-index (not a
skip) purely to backfill `reference_edges` — expected, one-time, and already how the `2`
bump behaved for `chunks`.

## 2. Indexes

| Index | Serves |
|---|---|
| `ix_reference_edges_target_name` (btree) | The resolver's name-equality join (`symbols.name = reference_edges.target_name`), shipped in #86 (§4) |
| `ix_reference_edges_target_trgm` (GIN, `gin_trgm_ops`) | Partial/substring reference lookups, parity with `ix_symbols_name_trgm` |
| `ix_reference_edges_file_id` (btree) | The per-file delete-and-reinsert writer (#84) and the `ON DELETE CASCADE` fired by the sweep/reconcile paths — Postgres does not auto-index a foreign key, and both are hot paths |
| `ix_reference_edges_repo_kind` (btree, `(repo_id, edge_kind)`) | Per-repo kind scans, e.g. `list_imports_payload`'s required `repo` scope (§4, #86) |

## 3. Deploy coupling — this migration is NOT schema-only

Same shape as `repo_branches` (0003) and `chunks` (0004) before it: `reference_edges` is a
new table, and the app/job grant builders in `app/db/grants.py` are schema-wide
(`GRANT ... ON ALL TABLES IN SCHEMA` + `ALTER DEFAULT PRIVILEGES`), so no code change was
needed for them to cover it. Whether a grant re-run is actually **required** after `0005`
depends on Postgres's `ALTER DEFAULT PRIVILEGES` (ADP) semantics:

- **ADP binds to the role that executed it**, not to the schema. `scripts/deploy.sh full`
  (`make deploy`) runs both the migrate step and the grants step as the same deploying
  identity, so on a fresh deploy the app/job roles get `SELECT` / `INSERT,UPDATE,DELETE`
  on `reference_edges` automatically the moment it's created — **no re-grant needed**.
- **A schema-only `make migrate TARGET=<target>` run by that SAME identity** against an
  already-deployed target is also covered automatically, for the same reason.
- **A schema-only migrate run by a DIFFERENT identity** than the one that originally ran
  `ALTER DEFAULT PRIVILEGES` is **not** covered — ADP simply never fires for that role, so
  the app/job roles get nothing on the new table.

This is proven in CI (`tests/integration/test_migrations.py`:
`test_reference_edges_adp_same_role_covers_new_table` and
`test_reference_edges_adp_different_role_does_not_cover_new_table`), not assumed.

**Always deploy this with `scripts/deploy.sh full` (i.e. `make deploy`) or, for an
already-deployed target migrated by a different identity, re-run the grants step
explicitly:**

```
APP_SP_ROLE=<app-sp-client-id> JOB_WRITER_ROLE=<job-run-as-sp-client-id> \
  make migrate TARGET=<target> ARGS=--apply-grants
```

### Verifying the grant landed

Run as the deploying identity (or any role with `SELECT` on `pg_catalog`) against the
target:

```sql
SELECT has_table_privilege('<app-sp-client-id>', 'reference_edges', 'SELECT'),
       has_table_privilege('<job-run-as-sp-client-id>', 'reference_edges', 'INSERT');
```

Both must return `true`. If either is `false`, run the re-grant command above — it is
idempotent, safe to run against a target that's already current.

## 4. Query-time resolution (#86)

`app/search/references.py` resolves a raw `reference_edges` row's `target_name` to the
`symbols` rows it could plausibly mean, entirely at query time — nothing from this
resolution is ever written back to `reference_edges` or `symbols`, and no extraction-time
symbol FK was added. `resolve_references(conn, ...)` is the entry point; `app/service.py`
wraps it in two additive payload builders, `find_references_payload` (corpus-wide,
`edge_kind="call"`) and `list_imports_payload` (`edge_kind="import"`).

**MCP tools shipped in #87.** Both builders are now exposed as MCP tools in `app/main.py`:
`find_references(symbol, limit, branch)` and `list_imports(repo, target, direction, branch,
limit)`. `list_imports` gained a `direction` parameter (an additive, signature-compatible
extension of the builder): `direction="imports"` enumerates a repo's import sites (`repo`
required), and `direction="imported_by"` finds who imports a given dotted `target`
corpus-wide (`target` required, index-served by `ix_reference_edges_target_name`). Invalid
input returns a structured payload (`unsupported_direction`/`missing_repo`/`missing_target`
with a `reason`), never an exception, validated PRE-DB in the service-layer builder so the #88
Web UI inherits it. The "what tests cover symbol X" question composes from the existing
primitive — `find_references(X)` plus a client-side `sites[].file` test-path filter, each
surviving site's `enclosing_symbol` naming the covering test — so no new tool was added.
Deferred past #87: repo/kind-scoped `find_references` filters, and per-file forward imports
("what does file F import").

**Two-query design, deliberately not one joined query.** Query 1 selects matching edge
sites from `reference_edges`/`files`/`repos`; query 2 selects candidate `symbols` for the
distinct `target_name`s query 1 returned, from `symbols`/`files`/`repos`. Neither query
references the other's tables. A single `reference_edges JOIN symbols ON target_name =
name` (both reaching through `files`/`repos`) would let SQLAlchemy auto-correlate the two
`files`/`repos` legs against each other, silently mis-scoping which candidate belongs to
which site — the same auto-correlation hazard `app/search/symbols.py` avoids for `sym:`
lookups. Query 2 bounds its **fetch itself** (not just the returned payload) with a SQL
window function per `target_name` (`ROW_NUMBER() OVER (PARTITION BY symbols.name ORDER BY
...)`, capped at `DEFAULT_CANDIDATE_CAP = 32`), so a hot name (`get`, `run`, `__init__`)
never pulls its entire corpus-wide match set into memory. `COUNT(*) OVER` carries the TRUE
pre-cap count alongside the trimmed rows, so ambiguity is never rewritten to "unique" just
because the candidate list was capped.

**Candidate-set contract.** Each edge site resolves to zero, one, or many ranked
candidates — `resolution` is `"unresolved"` (0), `"unique"` (1), or `"ambiguous"` (2+),
derived from the true pre-cap `candidate_count`. Ranking (same-repo before cross-repo, then
kind-appropriate, then same-file, tiebreaking on `(repo_id, path, start_line, symbol_id)`
for a deterministic total order) runs in Python after query 2, since every signal is
relational to the `(site, candidate)` pair. Ranking is **membership-preserving**: a
lower-ranked candidate is sorted later, never dropped, so genuine ambiguity is always
represented in full (up to the cap) rather than silently collapsed to one answer.

**Import edges resolve to their full dotted path, no last-segment split.** `import`
`target_name` is the complete dotted path as written (see §1); `symbols.name` is bare, so
an import edge resolves to a candidate only in the rare case a symbol is literally named
that full dotted string. This is pinned deliberately, not a gap: (1) it keeps one
exact-equality, index-served predicate identical for both edge kinds, with no functional
index and no client-side splitting; (2) a dotted import genuinely points at an
external/stdlib module most of the time, so representing it as `"unresolved"` (= external)
is *correct*, not a miss; (3) a last-segment heuristic (`a.b.get` → every symbol named
`get`) would manufacture false ambiguity and defeat precision. `list_imports_payload`'s
value is *enumerating* import sites with their `target_name`, not resolving them to local
definitions.

**`repo` is required for `direction=imports`; `direction=imported_by` is target-required,
index-served by `ix_reference_edges_target_name`.** A corpus-wide *bare* import listing
(`imports` with no `repo`) would filter on `edge_kind` alone — the trailing column of
`ix_reference_edges_repo_kind (repo_id, edge_kind)`, not index-served on its own — so it is
out of scope and returns a `missing_repo` validation payload. The `imported_by` direction is
the opposite case: it filters on `target_name` equality, which the btree
`ix_reference_edges_target_name` serves corpus-wide, so no `repo` is needed (one may still be
passed to narrow the result). `repo_known: False` is a structured "no such repo" miss (mirrors
`get_file_payload`'s `found: False`), distinguishable from a known repo with zero import sites
(`repo_known: True`, `sites: []`); it is always `True` when no `repo` scope was requested.

**Branch scoping matches `search_code`/`get_file` exactly**, applied independently to BOTH
the edge site's file and each candidate's file: an explicit `branch` uses
`files.branches @> ARRAY[:branch]`; omitted, it falls back to
`coalesce(repos.default_branch, 'HEAD') = ANY(files.branches)` — the same predicate
`get_file_payload` uses, asserted byte-identical in `tests/unit/test_references.py` and
exercised end-to-end in `tests/integration/test_references.py`.

**Quality measurement (`scripts/measure_reference_resolution.py`).** An offline script
reuses `app.search.references.build_candidate_count_select` and `classify_resolution` — the
SAME join semantics and branch predicate the live resolver's query 2 uses — so its
distribution agrees with the serve path by construction rather than re-implementing the
join. **`call` edges are the primary headline metric**, the only number compared against
the epic's deep-dive baseline (28.8% unique / 33.4% ambiguous / 37.8% external); the
`import`-edge distribution is reported separately, labeled informational (expected close to
0% resolution, validating the exact-dotted-match decision above). Run it with:

```
uv run python scripts/measure_reference_resolution.py --edge-kind both
```

Recorded distribution (self-indexed corpus: this repo's own git-tracked source tree —
206 files, 2,947 symbols, 15,412 reference edges across Python/JS/TS/TSX — default branch,
measured on 2026-07-23; see the #86 PR body for the full script output):

```
call edges -- HEADLINE AC4 metric (n=14226):
  unique         4144   29.1%   (baseline 28.8%)
  ambiguous      4208   29.6%   (baseline 33.4%)
  unresolved     5874   41.3%   (baseline 37.8%)

import edges -- informational, expected ~0% resolution (validates D3) (n=1186):
  unique           15    1.3%
  ambiguous         8    0.7%
  unresolved     1163   98.1%
```

The re-measured `call`-edge distribution tracks the baseline closely (within ~4 points on
every bucket); `import` edges resolve at ~2% total, confirming they are overwhelmingly
external/stdlib targets as D3 predicts.

## Reference

- [multi-branch.md §3](multi-branch.md#3-deploy-coupling--this-migration-is-not-schema-only) —
  the same grant-coupling pattern for `repo_branches` (0003).
- [semantic-enablement.md](semantic-enablement.md) — the same pattern for `chunks` (0004).
