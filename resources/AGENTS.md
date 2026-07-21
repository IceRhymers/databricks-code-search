<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# resources

## Purpose
Databricks Asset Bundle resource declarations, pulled in by `databricks.yml`'s
`include: resources/*.yml`. One file per resource family: the two Databricks Apps (MCP
server and web UI), the serverless indexing job, the Lakebase Autoscaling project, and
the secret scope for the GitHub token. Declarations are deliberately minimal — anything
auto-provisioned (the `production` branch, the `primary` endpoint, each app's service
principal) is *not* declared, because re-declaring it collides with the auto-created
object. Prod-only overlays (the `code-search-job-writer` Postgres role and the job
`run_as` SP) live under `targets.prod.resources` in `databricks.yml`, not here, so
their empty `job_run_as_sp` default never reaches `validate -t dev`.

## Key Files
| File | Description |
|------|-------------|
| `app.yml` | `apps.code_search` (`${var.app_name}`, default `mcp-code-search`): the FastMCP streamable-HTTP server. `source_code_path: ../app`, `compute_size: MEDIUM`. Binds Lakebase via a `postgres` resource (NOT `database` — that resolves against the provisioned Database Instance namespace and fails deploy for an Autoscaling project), building branch/database paths from `${resources.postgres_projects.pg_project.id}` so DABs orders the project first. `permission: CAN_CONNECT_AND_CREATE` is Postgres login only; the SELECT grant comes from `scripts/deploy.sh` step 7. No inline `config` — command/env live in `app/app.yaml` |
| `webui.yml` | `apps.webui` (`${var.webui_app_name}`, default `webui-code-search`): the browser-facing web UI, a second app mirroring `app.yml` — same project/branch/database binding (one shared read-only corpus), same `CAN_CONNECT_AND_CREATE`-is-login-only caveat (its grant is deploy.sh step 9), `source_code_path: ../webui`, no inline `config` (env lives in `webui/app.yaml`) |
| `job.yml` | `jobs.code_search_index` (`code-search-index`): serverless `python_wheel_task` running the `code-search-index` entry point from the `../dist/*.whl` artifact (serverless environment `client: "3"`). Named parameters: `config` (bare `${workspace.file_path}/config.yaml` — prefixing `/Workspace/` doubles the path, cli#2300), `max_repos`, secret `scope`/`key`, Lakebase `endpoint` path, `database`. Underscore keys (`max_repos`) because each key is emitted verbatim as `--<key>`. Scheduled by `${var.index_schedule}` (default `0 0 */12 * * ?` — every 12 hours, UTC) with queueing enabled |
| `lakebase.yml` | `postgres_projects.pg_project`: the Lakebase Autoscaling project (`${var.lakebase_project_name}`, `pg_version` 17), `default_endpoint_settings` 0.5–4 CU with 300s suspend. Branches/endpoints are NOT declared — the project auto-provisions `production` + READ_WRITE endpoint `primary`, and declaring them would force `replace_existing` on a data-bearing branch. A Postgres-backed UC catalog and the legacy `database_instances` fallback are kept as commented-out reference blocks |
| `secret_scope.yml` | `secret_scopes.gh_token` (`${var.github_token_secret_scope}`, default `code-search`, `backend_type: DATABRICKS`): the bundle creates the scope only; the GitHub token value is set out-of-band via `make set-secrets` after deploy |

## For AI Agents

### Working In This Directory
- **Relative paths resolve against this directory**, not the bundle root:
  `../app`, `../webui`, `../dist/*.whl` all reach up one level deliberately.
- Keep app→Lakebase bindings on the `postgres` resource with paths built from
  `${resources.postgres_projects.pg_project.id}` — the `.id` reference is what creates
  the DABs dependency edge (project created before app); a literal `projects/<name>`
  path loses the ordering. The `database` binding form does not work for Autoscaling
  projects.
- The database RESOURCE id is the hyphenated `databricks-postgres`; the connection
  database NAME is `databricks_postgres` (`var.database_name`). Do not "fix" one to
  match the other.
- Never declare `postgres_branches`/`postgres_endpoints` for the production branch, and
  never add inline `config` to either app (command/env belong in `app/app.yaml` /
  `webui/app.yaml`; declaring both risks merge conflicts and empty-resolving
  interpolation).
- App-level `permission: CAN_CONNECT_AND_CREATE` is only a Postgres login (and the
  sole enum the binding accepts). Table/schema grants are applied post-activation by
  `scripts/deploy.sh` (steps 7/9) via `scripts/migrate.py --apply-grants` — a new app
  resource here needs a matching grants step there.
- Job named-parameter keys must match the entry point's argparse flags exactly
  (underscores, not hyphens).
- The prod-only `postgres_roles.job_writer` (`role_id: code-search-job-writer`,
  `postgres_role: ${var.job_run_as_sp}`) and the job `run_as` overlay live in
  `databricks.yml` under `targets.prod` — add prod-only resources there, not here.
- Files the job/apps need at runtime must survive bundle sync: `.gitignore` applies
  silently to sync (cli#3547), so runtime files (`config.yaml`, `webui/uv.lock`,
  `webui/wheels/*.whl`) are force-included in `databricks.yml`'s `sync.include`.

### Testing Requirements
- No unit tests target these YAMLs. The gate is a live
  `databricks bundle validate -t dev` (and `-t prod` with `--var job_run_as_sp=...`);
  several keys are noted in-file as CLI-version-sensitive (`compute_size`, the
  serverless `client` version) — re-verify against validate after CLI upgrades.
- End-to-end exercise is `make deploy TARGET=dev` (deploys and activates everything
  declared here) followed by `make smoke` — `/ready` is the oracle that the binding +
  grant chain actually works.
- The `ci` target overrides only `lakebase_project_name: code-search-ci`; these same
  resource files back the (currently disabled) `ci-lakebase.yml` workflow.

### Common Patterns
- Every tunable is a `${var.*}` reference with its default and description in
  `databricks.yml` — add a variable there rather than hard-coding values here.
- `${resources.<type>.<name>.id}` references for cross-resource paths (ordering
  edges); `${workspace.file_path}` for workspace paths (already absolute).
- Heavy header comments carry the *why* (issue numbers, failure modes, verified-live
  notes); alternatives are preserved as commented-out blocks (see `lakebase.yml`).
  Keep that style when editing.
- Minimal-declaration principle: declare only what auto-provisioning does not create.

## Dependencies
- Internal: `databricks.yml` (variables, `include`, `artifacts.wheel`, sync includes,
  prod overlays), `app/` and `webui/` source trees plus their `app.yaml` runtime
  configs, `dist/*.whl` built by `uv build --wheel`, `config.yaml` read by the job at
  runtime, and `scripts/deploy.sh` which owns activation ordering and grants for
  everything declared here.
- External: Databricks Asset Bundles (`databricks` CLI) and the workspace surfaces
  they drive — Databricks Apps, serverless Jobs, Lakebase Autoscaling
  (`postgres_projects`), and Databricks-backed secret scopes.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
