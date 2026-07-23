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
knowledge-graph epic (#82): a later child (#86) resolves `target_name` to a concrete
`symbols` row at **query time**, by name-join — this table deliberately carries **no
foreign key to `symbols`**. Symbol ids churn on every per-file delete-and-reinsert, and an
FK would couple the two rewrite orders inside the indexing transaction for no query
benefit.

The enclosing symbol (the function/class a call or import site sits inside, if any) is
denormalized onto the row as `enclosing_name` / `enclosing_kind` / `enclosing_start_line`
/ `enclosing_end_line`, all nullable — `NULL` means module/top-level scope, exactly the
same convention `symbols` and `chunks` use elsewhere. There is no `branches` column and no
`commit` column: branch membership rides `files.branches` at query time (the resolver
joins through `files` with the same `coalesce(default_branch,'HEAD')` conjunct used
everywhere else), and `files.commit` is documented-ambiguous under multi-branch dedup and
must gain no new readers.

**This table is dormant until #84 ships a writer.** `0005` only creates the schema; no
code path inserts rows yet. `indexer/store.py`'s cascade-owning functions (`index_repo`'s
membership sweep, `reconcile_retired_branches`, `reconcile_removed_repos`) already
enumerate `reference_edges` alongside `symbols`/`chunks` in their docstrings and rely on
the same FK-cascade mechanism proven in §7.2 of the design doc and in
`tests/integration/test_reconcile.py` / `test_store.py` — no behavior change was needed to
make the cascade correct, because both `repos -> reference_edges` and
`files -> reference_edges` are `ON DELETE CASCADE` foreign keys.

## 2. Indexes

| Index | Serves |
|---|---|
| `ix_reference_edges_target_name` (btree) | The resolver's name-equality join (`symbols.name = reference_edges.target_name`) once #86 ships |
| `ix_reference_edges_target_trgm` (GIN, `gin_trgm_ops`) | Partial/substring reference lookups, parity with `ix_symbols_name_trgm` |
| `ix_reference_edges_file_id` (btree) | The per-file delete-and-reinsert writer (#84) and the `ON DELETE CASCADE` fired by the sweep/reconcile paths — Postgres does not auto-index a foreign key, and both are hot paths |
| `ix_reference_edges_repo_kind` (btree, `(repo_id, edge_kind)`) | Per-repo kind scans (e.g. a future `list_imports` MCP tool) |

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

## Reference

- [multi-branch.md §3](multi-branch.md#3-deploy-coupling--this-migration-is-not-schema-only) —
  the same grant-coupling pattern for `repo_branches` (0003).
- [semantic-enablement.md](semantic-enablement.md) — the same pattern for `chunks` (0004).
