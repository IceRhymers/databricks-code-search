# databricks-code-search

An agent-facing code search service on Databricks: a scheduled job indexes GitHub
repositories into Lakebase Postgres, and an MCP server exposes that corpus to agents
over streamable HTTP with a zoekt-style query language.

It exists so an agent can grep a codebase it has never cloned. The corpus is shared and
read-only at query time; there is no per-user filtering, so only index repositories every
caller of the MCP endpoint is allowed to read.

## Architecture

<img src="docs/diagrams/architecture.png" alt="GitHub to indexing job to Lakebase Postgres to MCP server to MCP client" width="450">

The job resolves each repo's default branch (plus any extra branches `config.yaml`
declares, see [Configuring what gets indexed](#configuring-what-gets-indexed)) to an
immutable SHA per branch, downloads the tarball for that SHA (no git binary — the job runs
on serverless), parses every source file, extracts symbols with tree-sitter, and writes
files and symbols in a single transaction per `(repo, branch)`, stamped with that SHA.
Content shared byte-for-byte across branches dedupes into one row carrying every branch
that resolves to it; a branch's stale membership is then swept from rows it no longer
resolves to, so a failed run rolls back whole rather than leaving the corpus
half-updated.

The server holds one process-scoped SQLAlchemy engine over a 5-connection pool, minting a
fresh Lakebase OAuth token on each physical connection. Query work runs off the event loop
under a 5-token limiter sized to the pool.

## Query language

`search_code` takes a zoekt-style query. Six fields are supported:

| Field | Meaning | Example |
|---|---|---|
| `repo:` | repository name, always case-insensitive | `repo:acme` |
| `file:` | file path | `file:src/` |
| `lang:` | language, lowercased; unknown values match nothing | `lang:go` |
| `sym:` | symbol name (correlated `EXISTS` over `symbols`) | `sym:Handler` |
| `branch:` | exact branch-membership match (GIN-served `@>`, not a glob/regex) | `branch:main` |
| `case:` | `yes` or `no`; query-global, last one wins | `case:yes Foo` |

Values may be bare (`repo:acme`), quoted (`repo:"my repo"`), or regex
(`file:/foo\/bar/`). `repo:`, `file:`, and `sym:` values are treated as regex patterns and
are never escaped. `branch:` is the one exception — its value is matched **exactly**
against a file's real branch membership, never as a regex or glob, and diverges from
`repo:`'s `~*` semantics on purpose (see
[`docs/runbooks/multi-branch.md`](docs/runbooks/multi-branch.md#4-branch-query-semantics--exact-not-a-glob)).

Content terms are substrings (`foo`, or `"a b"` to keep spaces) or regexes (`/Foo.*Bar/`).
Matching is **case-insensitive by default** — there is no smart-case inference like zoekt's.

### Branch scoping

Without `branch:` (or the `branch` tool parameter below), results are scoped to **each
repo's default branch only** — a file present on a non-default branch never surfaces
unless a query explicitly asks for it. `branch:<name>` (or `branch=<name>` on
`search_code`/`semantic_search`/`get_file`) restricts to files whose indexed branches
include `<name>`, exactly. Which non-default branches are indexed at all is a
`config.yaml`-time decision (`branches:` globs, capped at 20 per repo) — see
[Configuring what gets indexed](#configuring-what-gets-indexed) and
[`docs/runbooks/multi-branch.md`](docs/runbooks/multi-branch.md) for the indexer/deploy
side of multi-branch support.

Whitespace means AND, `OR` (any case) means OR, and AND binds tighter:

```
a b OR c d          ==  (a AND b) OR (c AND d)
(a OR b) (c OR d)   ==  parentheses override precedence
repo:acme lang:go /Foo.*Bar/
```

Not supported in V1. Most of these raise; the first two are silent, which is the more
dangerous case:

- **Negation.** `-foo` parses as a literal substring, with no error. If negation ships
  later, queries written today will silently flip meaning.
- **`AND` as a keyword.** `a and b` silently searches for the literal word `and`.
- **`content:`**, and the single-letter aliases `r` `f` `l` `b` `c` `s` — reserved, and
  raise a parse error. (`branch:` is no longer reserved — see the field table above.)
- **Dangling `OR`** (`a OR`, `OR a`) and **empty groups** (`()`) raise.

### Regex is not RE2

Two different engines run in sequence, and neither is zoekt's RE2:

1. Postgres POSIX ARE (`~` / `~*`) selects which files match. Invalid patterns surface as
   a query-time database error, not a parse error.
2. Python `re` rescans those files to produce the highlighted line matches.

The practical consequences: `^` and `$` are line anchors and `.` never crosses lines;
a Postgres-valid pattern that Python `re` rejects contributes no highlights *for that atom*
and sets `regex_incompatible`, so a single-atom query of that shape returns nothing while
other atoms in the same query still match; and case folding can disagree on non-ASCII pairs
(`ß`/`SS`, Turkish dotless `ı`). ASCII is unaffected.

Line matching is also **highlight-driven** — a file appears only if some line produces a
non-empty highlight. A content-free filter query like `lang:go` on its own, or a zero-width
pattern like `/^/`, returns nothing even though the SQL predicate matched. `sym:` is the
exception: `search_code` runs a separate symbol leg, so `sym:Handler` returns definitions
(carrying `symbols` and a `line`, but empty `text`) rather than falling into this hole.

One more V1 limitation worth knowing before pointing agents at it: a
catastrophic-backtracking regex on a single under-cap file runs unbounded in Python and can
stall the server. `statement_timeout` bounds the database, not the rescan.

## MCP tools

| Tool | Parameters | Returns |
|---|---|---|
| `search_code` | `query`, `limit=200`, `branch=None` | file-grouped line matches with byte ranges |
| `semantic_search` | `query`, `limit=50`, `branch=None` | ranked chunks with `rrf_score` |
| `list_repos` | — | indexed repos with per-branch last-indexed metadata |
| `get_file` | `repo`, `path`, `branch=None` | full file content, or `found: false` |

Every tool returns a JSON string. `limit` is clamped server-side: a non-positive value
falls back to 200, and anything above 1000 is capped there.

`branch` behaves differently per tool because `search_code` takes zoekt grammar and
`semantic_search` takes natural language: on `search_code` it is sugar for appending
`branch:"<value>"` to the query string (quoted, so `/`, `.`, and spaces need no escaping
of their own); on `semantic_search` it threads straight to the SQL predicate, never into
the free-text query. Omitted on either, results scope to each repo's default branch. On
`get_file`, `branch` disambiguates when a path has more than one indexed content version
(divergent branches) and the response's `branch` field reports which one was resolved —
never the literal string `"HEAD"` unless that is genuinely the resolved branch (e.g. a
repo with no `default_branch` recorded).

Recoverable conditions come back as payload fields —
`query_parse_error`, `query_too_broad`, `truncated`, `regex_incompatible` — rather than
errors, so an agent can react without a failed tool call.

`semantic_search` is natural-language hybrid search (vector ANN + BM25 fused by reciprocal
rank). It is registered unconditionally but gated at runtime: when disabled, which is the
default, it returns `semantic_enabled: false` and touches neither the database nor the
embedder. Turning it on means `CODE_SEARCH_SEMANTIC_ENABLED=1` *and* a separate,
irreversible migration — see
[`docs/runbooks/semantic-enablement.md`](docs/runbooks/semantic-enablement.md).

Two HTTP routes sit alongside the MCP mount: `GET /health` is liveness and never touches
the database, and `GET /ready` runs `SELECT 1 FROM repos LIMIT 1` so that a role holding
connect-but-not-select fails as 503 instead of shipping green.

## Web UI

`webui` (issue #35) is a second Databricks App, deployed and activated by the same
`make deploy` pipeline as the MCP server. It is a browser-facing search UI: a FastAPI
backend that imports the same `app.*` search stack in-process (own engine singleton, same
`/api/search` → `search_code_payload` path plus keyset-cursor pagination for "load more"),
and a React/Vite frontend with a committed production build
(`webui/frontend/dist/` — no Node needed to deploy or run CI). It reads the same Lakebase
corpus as the MCP app, read-only, via its own service principal and its own least-privilege
grant.

Auth is plain workspace `CAN_USE` on the app — no OAuth app connection, no MCP client setup;
open the app URL in a browser. See
[`docs/runbooks/webui.md`](docs/runbooks/webui.md) for the app URL lookup, the grants detail,
rebuilding the frontend (`make webui-build`), and the wheel-packaging mechanism that lets
webui import `app.*` without duplicating it.

## Out-of-band prerequisites

Four things the bundle cannot create. The first three gate a working MCP endpoint; the
fourth only matters for semantic search.

**1. Pre-created service principals.** An account admin creates the service principals
once; their client IDs become `app_sp_client_id` and `job_run_as_sp`. The bundle does not
create service principals. For prod, `make deploy-prod` refuses to run unless
`JOB_RUN_AS_SP=<client-id>` is set — that SP is declared as the `code-search-job-writer`
Postgres role and is what the indexing job runs as.

**2. Account-admin OAuth app connection.** MCP client auth on Databricks Apps is
**OAuth-only — there is no PAT path**. An account admin must register a Databricks OAuth
app connection (Account Console → Settings → App Connections) with the client's redirect
URLs, e.g. `http://localhost:<port>/oauth/callback` for Claude Code or Claude Desktop.
Until this exists, no external MCP client can reach `/mcp` over native OAuth — requests get
an OAuth redirect instead of a response.

This prerequisite is **avoidable**: the recommended client setup uses `uc-mcp-proxy`, which
borrows your Databricks CLI credentials instead of running the MCP OAuth flow, so it needs
no app connection and no redirect URLs. Only take on prerequisite 2 if you specifically want
native per-client OAuth. See [Connecting a client](#connecting-a-client).

`make smoke ARGS=--enable-mcp` is not blocked by this: it authenticates with your own
Databricks login (`WorkspaceClient().config.authenticate()`), so it needs `CAN_USE` on the
app but not the app connection. `deploy.sh`'s step-8 reminder says no client can reach
`/mcp` without the app connection; that holds for external MCP clients but not for
`make smoke`.

**3. GitHub token.** `make set-secrets` writes `GITHUB_TOKEN` into the bundle-created
secret scope (`code-search` / `github_token` by default). `make deploy` will do this for
you if `GITHUB_TOKEN` is exported; without it the deploy still succeeds and the app still
serves, but indexing has no credential and the corpus stays empty.

**4. Lakebase Search beta.** Semantic search only. The Lakebase project's
Databricks-managed `shared_preload_libraries` must already include
`lakebase_vector,lakebase_text`. This is not settable through the bundle or the API, it is
requested out-of-band per project, and **it is irreversible**.

## Deploy

> **Step 1 after cloning: replace `IceRhymers` with your own account.** `config.yaml`
> ships pointed at the repo author's GitHub user. Deployed unedited it indexes a
> stranger's repos every 12 hours and your searches return their code — successfully,
> with no error anywhere. See [Configuring what gets indexed](#configuring-what-gets-indexed).

```bash
make install                      # uv sync --all-groups
export GITHUB_TOKEN=ghp_...       # so deploy can seed the secret scope
make deploy TARGET=dev            # full ordered pipeline
```

For prod, the job run-as SP is mandatory:

```bash
JOB_RUN_AS_SP=<client-id> make deploy-prod
```

`make deploy` runs `scripts/deploy.sh full`, which deploys and activates **both** Databricks
Apps in the bundle — the MCP server (`code_search`) and the [web UI](#web-ui) (`webui`):

<img src="docs/diagrams/deploy-pipeline.png" alt="The eleven steps of deploy.sh full, with the migrate and grant steps split around each app's activation" width="720">

1. **Validate** the bundle; for prod, assert `JOB_RUN_AS_SP` is non-empty.
2. **Build the webui wheel** (`make webui-wheel`) so webui's source sync ships a fresh
   `app.*` import.
3. **Deploy** resources — Lakebase project, UC catalog, secret scope, job, both apps.
   Compute is not started yet.
4. **Seed the GitHub secret** if it is missing and `GITHUB_TOKEN` is set; otherwise warn
   and continue.
5. **Migrate** the schema as the deploying identity, *without* grants.
6. **Activate the MCP app** via `bundle run`, then poll for `ACTIVE` (15s × 10).
7. **Apply grants — MCP app** — read-only for its app SP, write for the job SP on prod.
8. **Activate webui** via `bundle run`, then poll for `ACTIVE` (15s × 10).
9. **Apply grants — webui** — read-only for its app SP.
10. **Index** — always runs; a failure warns without aborting the deploy.
11. **Print both app URLs** and the reminder about the MCP app's OAuth app connection.

Steps 5/7 and 8/9 are split the same way for each app: a service principal's Postgres role
does not exist until that app first activates, so granting before activation cannot work —
each grant pass runs after its app's activation step and retries (5 × 10s) to absorb
role-visibility lag.

If step 6 or step 8 never reaches `ACTIVE`, the script falls back to
`databricks apps deploy <app> --source-code-path`, re-runs `bundle run`, and re-probes. The
script calls this the first-activation fallback.

## Configuring what gets indexed

`config.yaml` at the repo root is the single source of truth for the indexed repo set. It
is git-versioned, edited by hand, and synced to the workspace by `bundle deploy`; the job
reads it and resolves the concrete repo list on **every run**, so a new repo in a declared
org appears on the next 12-hour tick with no redeploy. Changing the config itself does need
a `make deploy` — that is what re-syncs the file.

```yaml
version: 1

connections:
  - type: github
    users:
      - IceRhymers
    orgs:
      - acme
    repos:
      - otherorg/specific-repo
    branches:
      - "release/*"
    exclude:
      forks: true
      archived: true
      repos:
        - "acme/test-*"
      size_mb: 500
```

`users`, `orgs`, and `repos` are **unioned**, then deduplicated by canonical `org/repo`.
`users` and `orgs` are expanded through the GitHub API at runtime; `repos` entries are
taken verbatim with no enumeration call.

### `branches:` — indexing more than the default branch

`branches` is a list of glob patterns (`fnmatchcase`, exact-name match — not a regex)
matched against each repo's branch list, **in addition to** its default branch, which is
always indexed regardless of match. Empty (the default, and every config that predates
this feature) means default-branch-only, with no behavior change and no extra GitHub API
call. Resolved branches are capped at 20 per repo, truncated default-first-then-alphabetical
with a loud warning if a glob matches more. See
[`docs/runbooks/multi-branch.md`](docs/runbooks/multi-branch.md) for the cap/truncation
details, the deploy-grant coupling this feature introduces, and how `branch:`-scoped
queries reach this indexed set at query time.

### `exclude` rules

| Key | Default | Semantics |
|---|---|---|
| `forks` | `true` | Drops repos GitHub reports as forks. A fork and its upstream are *distinct* dedup keys, so keeping both doubles the corpus and pollutes ranking with duplicate hits. |
| `archived` | `true` | Drops archived repos. Their SHA never changes, but the indexer re-downloads, re-parses, and (with semantic on) re-embeds them every run regardless — so they cost full price forever. Set `false` if you want them anyway. |
| `repos` | `[]` | fnmatch globs matched against the canonical `org/repo` string, e.g. `"acme/test-*"` or `"acme/*-deprecated"`. |
| `size_mb` | `null` (no cap) | Drops repos larger than this. **GitHub reports repo size in KB; this field is MB** (the comparison is `size_kb > size_mb * 1000`). Note it measures the *git directory including history*, not a tarball of HEAD — a repo with long history and a small working tree can exceed a cap its checkout would not. |

> **Explicit always wins.** `exclude` filters **only** repos discovered through `orgs` and
> `users`. Anything listed by hand under `repos:` bypasses all four rules — an
> `exclude.repos: ["acme/test-*"]` glob will not remove a hand-listed `acme/test-harness`.
> Listing a repo explicitly is an unambiguous instruction, and honouring the filters would
> also force a metadata request per entry purely to apply a rule you did not ask for. To
> drop an explicit repo, delete its line.

Resolution is fail-fast and happens before anything is fetched: a failed enumeration, a
config that resolves to zero repos, or one that resolves past the `max_repos` ceiling
(default 500, raise it via the `max_repos` bundle variable) each exits non-zero having
indexed nothing. Every run logs a per-connection breakdown and the resolved repo names at
INFO — the fastest way to confirm you are indexing what you think you are.

Index on demand with `make index TARGET=dev`; otherwise the job runs every 12 hours.

## Smoke test

```bash
make smoke TARGET=dev                              # health, ready, connectivity
make smoke TARGET=dev ARGS=--expect-indexed        # also assert the corpus is non-empty
make smoke TARGET=dev ARGS=--enable-mcp            # also drive a real search_code call
```

`--enable-mcp` needs a populated corpus. `/ready` is the grant
oracle here — the direct-SQL check runs as the deploying identity and proves connectivity
only, so it will pass even when the app SP is missing its SELECT grant.

## Connecting a client

The server speaks streamable HTTP at `https://<app-url>/mcp`. Every caller needs `CAN_USE`
on the app, whichever path below you take.

Get the URL — `make deploy` prints it at step 8, and afterwards:

```bash
databricks apps get <app-name> -o json | jq -r '.url'
```

There are two ways to authenticate. They differ in who has to do setup work, not in what the
agent sees:

| | [`uc-mcp-proxy`](#option-a-uc-mcp-proxy-recommended) | [Native OAuth](#option-b-native-oauth-app-connection) |
|---|---|---|
| Transport to the client | stdio (proxied) | streamable HTTP |
| Account-admin setup | none | app connection + redirect URLs |
| Credentials | your Databricks CLI profile | per-client OAuth client ID |
| Works in every MCP client | yes | only clients Databricks documents |

### Option A: `uc-mcp-proxy` (recommended)

[`uc-mcp-proxy`](https://github.com/IceRhymers/uc-mcp-proxy) is a stdio-to-streamable-HTTP
shim that attaches a Databricks OAuth bearer from your **existing CLI profile** — the same
trick `make smoke` uses. Because it never runs the MCP OAuth flow, it needs no app
connection and no redirect URLs, which skips prerequisite 2 entirely.

Authenticate the CLI once:

```bash
databricks auth login --host https://<workspace-host>
```

Then add the server. Claude Code:

```bash
claude mcp add code-search -- uvx uc-mcp-proxy --url https://<app-url>/mcp
```

Any client that reads an `mcpServers` block — Claude Desktop, Cursor, Windsurf, VS Code —
takes the same command as JSON:

```json
{
  "mcpServers": {
    "code-search": {
      "type": "stdio",
      "command": "uvx",
      "args": ["uc-mcp-proxy", "--url", "https://<app-url>/mcp"]
    }
  }
}
```

`uvx` fetches the proxy on demand, so there is nothing to install; `uv tool install
uc-mcp-proxy` pins it locally if you would rather not pay the fetch on every launch.

Useful flags:

- `--profile <name>` — pick a non-default CLI profile. Worth setting explicitly if you have
  several workspaces configured, since the default profile is easy to lose track of.
- `--auth-type databricks-cli` — force CLI-profile auth when ambient
  `DATABRICKS_*` environment variables would otherwise be picked up first.
- `--no-auto-login` — for CI and headless runs. On an OAuth U2M profile with an expired
  token the proxy otherwise shells out to `databricks auth login` and opens a browser,
  which hangs a non-interactive job. Supply `DATABRICKS_TOKEN` (or M2M client
  credentials) instead.

Note that auto-login fires **only** for OAuth (`databricks-cli`) profiles. On PAT, M2M, or
Azure profiles the proxy reports the failure rather than re-running login, so it cannot
overwrite credentials you did not ask it to touch.

### Option B: native OAuth app connection

Use this if you want the client to hold its own OAuth registration rather than ride on your
CLI profile. It requires prerequisite 2 to be done first: an account admin registers an app
connection (Account Console → **Settings → App Connections**) carrying the client's redirect
URL and either `all-apis` or a narrower scope set. The CLI equivalent:

```bash
databricks account custom-app-integration create --json '{
  "name": "code-search-mcp",
  "redirect_urls": ["http://localhost:8080/oauth/callback"],
  "confidential": false,
  "scopes": ["all-apis"]
}'
```

That returns the client ID the client below needs. Claude Code:

```bash
claude mcp add-json code-search \
  '{"type":"http","url":"https://<app-url>/mcp","oauth":{"clientId":"<client-id>","callbackPort":8080}}'
```

The `callbackPort` must match the port in the registered redirect URL, or the browser
round-trip dead-ends after consent.

Machine-to-machine callers skip the redirect entirely and authenticate with
`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET` on a service principal holding
`CAN_USE`.

Two limits are worth knowing before you commit to this path. Databricks does not support
**dynamic client registration**, so clients that only speak DCR cannot use OAuth against
this endpoint at all — those need Option A. And **PAT auth does not work here**: Databricks
supports bearer-token auth for managed MCP servers, but Apps-hosted servers like this one are
OAuth-only, so an `Authorization: Bearer <pat>` header gets you the login redirect, not JSON.

### Troubleshooting

**A 302 where you expected JSON** means the request was unauthenticated. Under Option A,
your CLI token is missing or expired — re-run `databricks auth login`. Under Option B, it is
the classic symptom of a missing or misconfigured app connection.

**403 after a successful login** is authorization, not authentication: the identity reached
the app but lacks `CAN_USE`. Grant it in the app's permissions.

**Tools list, but every search returns nothing.** The corpus is empty rather than the
connection broken — check with `make smoke TARGET=dev ARGS=--expect-indexed`, and see
[Configuring what gets indexed](#configuring-what-gets-indexed).

## Local development

Requires Python 3.12+ and `uv`.

```bash
make install
make test                 # unit + observability; no external dependencies
make test-integration     # needs Postgres
make lint                 # ruff check + ruff format --check + mypy (incl. webui)
```

For the webui frontend specifically (requires Node):

```bash
make webui-build           # npm ci + vite build -> webui/frontend/dist/ (commit the result)
make webui-test            # vitest; advisory, not a repo gate
```

The integration suite needs a Postgres with `pg_trgm`; CI uses `pgvector/pgvector:pg16`
with `PGHOST`/`PGPORT`/`PGUSER`/`PGPASSWORD`/`PGDATABASE` pointed at it. Do **not** run
`make migrate-local` first — the fixtures build their own schema, and a pre-migrated
database fails the suite on a duplicate-key violation against `repos_name_key`.
`make migrate-local` is for running the app locally, not the tests.

`LAKEBASE_ENDPOINT` and `PGHOST` are precedence-ordered, not exclusive: a configured
`LAKEBASE_ENDPOINT` always wins; `PGHOST` selects local mode only in its absence. That
ordering matters because the deployed app's Postgres binding injects `PGHOST` at runtime,
so both are set in production. Locally the risk runs the other way, which is why
`make smoke` and `make migrate` refuse to run with `PGHOST` set — otherwise a stale shell
variable could quietly point them at your laptop instead of the deployment.

Run the server locally with `make run` (binds `DATABRICKS_APP_PORT`, else 8000).

## Reference

- [`docs/runbooks/multi-branch.md`](docs/runbooks/multi-branch.md) — configuring and
  deploying multi-branch indexing (`branches:` globs, the 20-branch cap, `branch:`
  query semantics, the grant-coupling this migration introduces)
- [`docs/runbooks/semantic-enablement.md`](docs/runbooks/semantic-enablement.md) — turning
  on semantic search
- [`docs/runbooks/ci-lakebase.md`](docs/runbooks/ci-lakebase.md) — running CI against a
  real Lakebase engine
- [`docs/runbooks/webui.md`](docs/runbooks/webui.md) — the web UI app: auth, grants,
  rebuilding the frontend, wheel packaging
- `docs/diagrams/*.dot` — Graphviz sources for the images above. The PNGs are committed;
  edit the `.dot` and run `make diagrams` rather than touching them.
- `make help` — every target with its flags
