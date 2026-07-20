# Web UI app (issue #35)

Operator notes for `webui`, the second Databricks App in this bundle. It is a browser-facing
search UI (FastAPI backend importing the existing `app.*` search stack in-process, React/Vite
frontend) that sits alongside the MCP app (`code_search`) and reads the same Lakebase corpus,
read-only. It shares no code path with MCP client auth and needs none of the MCP app's
out-of-band OAuth app connection prerequisite.

## Deploy and URL

`make deploy` / `scripts/deploy.sh full` provisions and activates both apps in one pipeline
(see the README's Deploy section for the full step list). The webui app's URL is printed at
the final banner alongside the MCP app's, and afterwards:

```bash
databricks apps get "$(databricks bundle validate -t dev -o json | jq -r '.variables.webui_app_name.value')" -o json | jq -r '.url'
```

## Auth: workspace `CAN_USE`

The webui app has no separate auth story. It runs behind standard Databricks Apps
authentication: any identity with `CAN_USE` on the app resource can open it in a browser and
use the search UI. Grant it the same way as the MCP app's `CAN_USE` (app permissions in the
workspace UI, or `databricks apps update-permissions`). There is no OAuth app connection to
register — that prerequisite is specific to the MCP app's `/mcp` streamable-HTTP transport,
which webui does not expose.

## Read-only role

The webui app's service principal is granted the same least-privilege read-only role as the
MCP app's SP: `SELECT` on all tables in the schema, `USAGE` on the schema, applied via
`scripts/migrate.py --apply-grants` with `APP_SP_ROLE=<webui-sp-client-id>`
(`app/db/grants.py:build_app_grants`). `deploy.sh`'s post-activation grants step (step 9 of
`cmd_full`) does this automatically after webui reaches `ACTIVE`; it is idempotent, so a
manual re-run (`make migrate ARGS=--apply-grants` with `APP_SP_ROLE` set to the webui SP's
client id) is safe if grants ever need reapplying out of band. Like the MCP app, `/ready`'s
`SELECT 1 FROM repos LIMIT 1` is the grant oracle: a missing grant surfaces as a 503, not a
silent empty result.

## Rebuilding the frontend

The production frontend build (`webui/frontend/dist/`) is **committed** — CI and deploy do
not need Node. To rebuild it after a frontend change:

```bash
make webui-build   # cd webui/frontend && npm ci && npm run build
```

Commit the resulting `webui/frontend/dist/` changes. `npm test` (vitest) is available via
`make webui-test`, advisory only — it is not wired into `make test` / `make test-integration`
and is not a repo gate.

## Packaging: the deploy-time wheel

`webui/` imports `app.*` (the search service layer, DB client, query compiler). Rather than
duplicate that code, `make webui-wheel` (`uv build --wheel`, then copied to the fixed path
`webui/wheels/app.whl`) stages a wheel that `webui/requirements.txt` references as a local
path dependency. `scripts/deploy.sh` runs `make webui-wheel` before `bundle deploy` on every
deploy, so the wheel is always freshly built from the current `app/` source — it is a build
artifact, not something you hand-maintain or commit.

This is sync-critical: Databricks Apps source sync respects `.gitignore` for
`source_code_path`, so the wheel must never be gitignored or it silently fails to upload and
the app crashes on import at start. `.gitignore` carries an explicit `!webui/wheels/*.whl`
negation as insurance against a future broad ignore rule breaking this silently.

**Human-deploy-gated verification.** Whether the Databricks Apps requirements installer
accepts a local-path wheel dependency (`./wheels/app.whl` in `webui/requirements.txt`) cannot
be verified without a live workspace deploy — it was not exercised in this PR. If the first
live `make deploy` shows the webui app failing to start with a dependency-resolution error,
the documented fallback is to change `webui/app.yaml`'s command to install the wheel
explicitly before starting uvicorn: `pip install wheels/app.whl && uvicorn ...`. Treat a
successful first `bundle run webui` / `ACTIVE` state as the confirmation this path works;
until then, flag it as an open verification item post-merge.
