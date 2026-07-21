<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# scripts

## Purpose
Operational scripts that the Makefile drives: the ordered deploy/destroy pipeline
(`deploy.sh`), the post-deploy smoke test (`smoke.py`), the Alembic migration + grants
runner (`migrate.py`), and the ephemeral Lakebase branch lifecycle for CI
(`ci_branch.py`). `deploy.sh` exists specifically to own ordering that must not be
reordered — everything that is a single command (smoke, index, set-secrets) lives in the
Makefile instead. The Python scripts import from `app/` (`app.db.client`,
`app.db.grants`) and are run via `uv run` with connection env (`LAKEBASE_ENDPOINT`,
`LAKEBASE_DATABASE`) resolved by the Makefile from `databricks bundle validate` JSON.

## Key Files
| File | Description |
|------|-------------|
| `deploy.sh` | `full`/`destroy` orchestrator. `full` is an 11-step pipeline: (1) validate bundle JSON, (2) `make webui-wheel`, (3) `bundle deploy`, (4) GitHub-token secret check/seed, (5) `make migrate` schema-only (no grants), (6) `bundle run code_search` + wait ACTIVE, (7) MCP-app SP grants (retry 5×10s), (8) `bundle run webui` + wait ACTIVE, (9) webui SP grants (retry 5×10s), (10) first index run (non-fatal), (11) URL banner + OAuth-app-connection reminder. `destroy` is a typed-confirm (project name) teardown. Helpers: `die`, `req` (empty/null guard), `retry`, `grant_attempt`, `jval`/`jworkspace` (guarded JSON readers), `wait_active` (~10×15s probe with `databricks apps deploy` fallback on first activation) |
| `smoke.py` | Post-deploy smoke test: `/health` (liveness), `/ready` (the grant oracle — proves the app SP's SELECT grant, which owner-side SQL cannot), direct-SQL `SELECT 1` (connectivity only), corpus count under `--expect-indexed`, and a live MCP `search_code` leg under `--enable-mcp` (https-only, validates the zoekt-parity envelope). Pure predicates at top are I/O-free and unit-testable; heavy imports (`httpx`/`mcp`/`sqlalchemy`/`databricks.sdk`) are lazy. All requests carry a fresh Databricks OAuth bearer (U2M or M2M). Never a silent green |
| `migrate.py` | Single Alembic entry point: opens the engine via `app.db.client.create_db_engine`, resolves one schema (`PGSCHEMA` or `current_schema()`) that pins search_path + version table + grants together, runs `upgrade head`. Grants are opt-in via `--apply-grants` and applied independently per role: `APP_SP_ROLE` (read-only) and `JOB_WRITER_ROLE` (write), each validated and asserted present in `pg_roles` first; at least one must be set |
| `ci_branch.py` | `up`/`down` lifecycle for an ephemeral Lakebase branch per CI run (real `lakebase_ann`/`lakebase_bm25` surface exists in no Postgres image). `up` forks `production` with `replace_existing=True` and a protobuf-`Duration` TTL (default 7200s cost backstop), resolves the auto-provisioned READ_WRITE endpoint (never creates one — the API rejects a second), prints `LAKEBASE_ENDPOINT`/`LAKEBASE_DATABASE` for `$GITHUB_ENV`. `down` purges best-effort and never fails the build |
| `.gitkeep` | Empty placeholder keeping the directory tracked |

## For AI Agents

### Working In This Directory
- **Step ordering in `deploy.sh` is the whole point of the file — do not reorder.**
  Grants (steps 7 and 9) must follow each app's activation (steps 6 and 8) because an
  app SP's Postgres role does not exist until the app's first activation; `migrate.py`
  asserts the role exists in `pg_roles` before granting, and the 5×10s `retry` only
  absorbs visibility lag, not a missing activation.
- Step 5 runs `make migrate` **without** grants deliberately (Decision A1): the
  developer identity owns the tables, and neither app SP role exists yet.
- Do not renumber the `[N/11]` steps without updating `README.md`, which references
  step numbers explicitly (steps 6/8 fallback, step 11 URL banner) and embeds
  `docs/diagrams/deploy-pipeline.png` ("the eleven steps of deploy.sh full").
- Prod requires `JOB_RUN_AS_SP` (the pre-created job run-as SP client id): `deploy.sh`
  dies without it, threads it as `--var job_run_as_sp=...`, and step 7 asserts **both**
  the app SP role and the job writer role before granting. The writer role comes from
  the guarded env, never from bundle JSON (which resolves the var to its `""` default).
- On dev, `grant_attempt` passes `JOB_WRITER_ROLE=""` — falsy, so `migrate.py` skips
  the job grant; a stray `JOB_WRITER_ROLE` in a developer's shell cannot leak.
- Every derived value goes through `req` (or `jq -er`): `jq -r`/`print()` emit
  `null`/`None` on missing fields and exit 0, which would otherwise leak into a grant
  target. Keep new derivations behind the same guards.
- First-activation fallback: if `bundle run` never reaches ACTIVE, push source via
  `databricks apps deploy <app> --source-code-path ...` and re-run once — keep this
  shape for any new app.
- `ci_branch.py`: the CI project's `production` branch must stay un-migrated (forks
  inherit extensions; `test_no_vector_extension_installed` depends on it).
- Never log role tokens/passwords in `migrate.py`; SDK imports stay lazy
  (`_client()` seam in `ci_branch.py`, `app.db.client` for credentials).

### Testing Requirements
- `smoke.py`'s pure predicates (`health_ok` … `validate_search_payload`) are covered by
  `tests/unit/test_smoke.py`; `ci_branch.py` by `tests/unit/test_ci_branch.py` — both
  run in `make test` with no live services.
- Live exercise: `make smoke TARGET=dev|prod` (refuses to run with `PGHOST` set), with
  `ARGS=--expect-indexed` after an index run and `ARGS=--enable-mcp` for the end-to-end
  MCP leg. Exit is non-zero on any gating failure.
- `migrate.py` is exercised by `tests/integration/test_migrations.py` against an
  ephemeral Lakebase branch; `ci_branch.py up`/`down` runs for real in
  `.github/workflows/ci-lakebase.yml` (currently disabled until the CI project is
  provisioned — see `docs/runbooks/ci-lakebase.md`).
- `deploy.sh` has no test harness; verify changes with `make deploy TARGET=dev`
  followed by `make smoke`.

### Common Patterns
- Bash: `set -euo pipefail`; `die` for every failure; `req` around every derived value;
  `retry <attempts> <sleep>` for eventually-consistent operations; `jq -er` (never bare
  `jq -r`) so missing fields fail instead of yielding `"null"`;
  `${VAR_ARGS[@]+"${VAR_ARGS[@]}"}` for safe empty-array expansion; JSON keys passed to
  python via argv, never interpolated into source.
- Python scripts: module docstrings carry the design rationale (issue numbers,
  pre-mortem decisions); pure predicates separated from live legs; heavy imports lazy
  inside functions; teardown paths never raise.
- Invocation is always through the Makefile (`make deploy`, `make deploy-prod`,
  `make migrate ARGS=--apply-grants`, `make smoke`), which resolves
  `LAKEBASE_ENDPOINT`/`LAKEBASE_DATABASE` from bundle-validate JSON per `TARGET`.

## Dependencies
- Internal: `app.db.client.create_db_engine` (OAuth engine), `app.db.grants`
  (`build_app_grants`, `build_job_grants`, `quote_ident`, `validate_role`); the
  Makefile (`migrate`, `webui-wheel`, `set-secrets` targets); `databricks.yml`
  variables (`app_name`, `webui_app_name`, secret scope/key, `job_run_as_sp`) and the
  resources in `resources/*.yml` that the pipeline deploys, runs, and grants.
- External: `databricks` CLI (bundle validate/deploy/run/destroy, apps get/deploy,
  secrets), `jq`, `python3`; Python: `alembic`, `sqlalchemy`, `databricks-sdk`
  (plus `google.protobuf` for the branch TTL), and lazily `httpx` + `mcp` in
  `smoke.py`. All Python runs under `uv run`.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
