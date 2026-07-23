# databricks-code-search

An agent-facing code search service on Databricks: a scheduled job indexes GitHub
repositories into Lakebase Postgres, and an MCP server exposes that corpus to agents
over streamable HTTP with a zoekt-style query language.

It exists so an agent can grep a codebase it has never cloned. The corpus is shared and
read-only at query time; there is no per-user filtering, so only index repositories every
caller of the MCP endpoint is allowed to read.

## Quick start

```bash
git clone git@github.com:IceRhymers/databricks-code-search.git && cd databricks-code-search
make install                 # needs Python 3.12+ and uv
$EDITOR config.yaml          # pick what to index — the template ships with nothing selected
export GITHUB_TOKEN=ghp_...  # so deploy can seed the secret scope
make deploy TARGET=dev       # full pipeline: deploy, migrate, activate, grants, first index
```

Deployed with `config.yaml` unedited, the indexer fails fast with `connection selects
nothing` and the corpus stays empty — uncomment `users:`/`orgs:`/`repos:` and fill in
your own first. See [Configuring what gets indexed](#configuring-what-gets-indexed).

`make deploy` needs an authenticated Databricks CLI (`databricks auth login`), and a few
one-time pieces cannot be scripted at all — those are next.

## Manual prerequisites

Four things the bundle cannot create, and which mostly need a human (or an account
admin). The first three gate a working MCP endpoint; the fourth gates
`make migrate`/`make deploy` (the core chain creates the semantic `chunks` schema).

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
app but not the app connection. `deploy.sh`'s step-11 reminder says no client can reach
`/mcp` without the app connection; that holds for external MCP clients but not for
`make smoke`.

**3. GitHub token.** `make set-secrets` writes `GITHUB_TOKEN` into the bundle-created
secret scope (`code-search` / `github_token` by default). `make deploy` will do this for
you if `GITHUB_TOKEN` is exported; without it the deploy still succeeds and the app still
serves, but indexing has no credential and the corpus stays empty.

**4. Lakebase Search preload (stated project assumption).** The Lakebase project's
Databricks-managed `shared_preload_libraries` must already include
`lakebase_vector,lakebase_text` — semantic search is default-on and its DDL rides
`make migrate`, which fails loudly (`must be loaded via shared_preload_libraries`) on a
project without it. Not settable through the bundle or the API; requested out-of-band per
project, and **irreversible**. See
[`docs/runbooks/semantic-enablement.md`](docs/runbooks/semantic-enablement.md).

## Setting this up with an agent

Setup splits cleanly between what a coding agent can do and what needs a human. If you
are an agent bootstrapping this project, do the following yourself:

1. Clone the repo and run `make install` (needs Python 3.12+ and `uv`).
2. Edit `config.yaml`: uncomment and fill `users:` / `orgs:` / `repos:` under
   `connections`. The file documents every knob inline.
3. Run `make lint && make test` to verify the checkout.
4. Deploy with `make deploy TARGET=dev` (add `PROFILE=<name>` for a non-default
   Databricks CLI profile).
5. Once you have a token (below): `export GITHUB_TOKEN=<token> && make set-secrets`,
   then `make index TARGET=dev` to populate the corpus.
6. Verify with `make smoke TARGET=dev ARGS=--expect-indexed`.

Ask your human for the pieces you cannot do yourself:

- **A GitHub token** that can read the repositories being indexed. The human only
  supplies the value; you write it into the secret scope with `make set-secrets`
  (scope `code-search`, key `github_token` by default).
- **Databricks CLI auth** — `databricks auth login --host https://<workspace-host>` is
  an interactive browser flow.
- **The account-admin prerequisites above** — service principals (always), the OAuth
  app connection (only for native MCP OAuth), and the Lakebase preload confirmation.

## Architecture

<img src="docs/diagrams/architecture.png" alt="GitHub to indexing job to Lakebase Postgres to MCP server to MCP client" width="450">

The job resolves each repo's default branch (plus any extra branches `config.yaml`
declares, see [Configuring what gets indexed](#configuring-what-gets-indexed)) to an
immutable SHA per branch, downloads the tarball for that SHA (no git binary — the job runs
on serverless), parses every source file, extracts symbols with tree-sitter, and writes
files and symbols in a single transaction per `(repo, branch)`, stamped with that SHA.
Content shared byte-for-byte across branches dedupes into one row carrying every branch
that resolves to it; a branch's stale membership is then swept from rows it no longer
resolves to, so a failure rolls back that `(repo, branch)` update whole rather than
leaving it half-applied (other repos and branches continue; the run still exits
non-zero).

The server holds one process-scoped SQLAlchemy engine over a 5-connection pool, minting a
fresh Lakebase OAuth token on each physical connection. Query work runs off the event loop
under a 5-token limiter sized to the pool.

The full deployment surface — one bundle carrying the job, the secret scope, and both
apps, each app reading through its own service principal's least-privilege grant, with
embeddings flowing through the workspace AI Gateway:

<img src="docs/diagrams/overall-architecture.png" alt="Overall architecture: config.yaml, GitHub, and the secret scope feed the indexing job, which writes to Lakebase Postgres and embeds chunks via the AI Gateway; the MCP server and web UI apps read Lakebase with read-only grants and serve an MCP agent over OAuth and a browser over CAN_USE respectively" width="700">

## Query language

`search_code` takes a zoekt-style query. Seven fields are supported:

| Field | Meaning | Example |
|---|---|---|
| `repo:` | repository name, always case-insensitive | `repo:acme` |
| `file:` | file path | `file:src/` |
| `lang:` | language, lowercased; unknown values match nothing | `lang:go` |
| `sym:` | symbol name (correlated `EXISTS` over `symbols`) | `sym:Handler` |
| `branch:` | exact branch-membership match (GIN-served `@>`, not a glob/regex) | `branch:main` |
| `commit:` | git hash (full SHA or ≥7-char hex prefix); scopes to the indexed branch heads at that commit | `commit:abc1234` |
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

### Commit scoping

`commit:<hash>` resolves a git hash — a full 40-char SHA or a hex prefix of **≥7 chars**,
matched git-style against `repo_branches.last_indexed_commit` (never `files.commit`, which
is write-only and ambiguous under multi-branch dedup) — to the (repo, branch) heads indexed
at that commit. Non-hex or too-short/too-long values are a query error. It has **two moods**:

- **Reverse lookup** — a bare `commit:<hash>` returns a `resolved` list (each `repo`,
  `branch`, full `commit`, `index_time`) and an **empty** `files` list: which branch heads
  are indexed at that commit.
- **Scoped search** — `commit:<hash> <terms>` runs a normal search scoped to the resolved
  heads and returns both `files` and the same `resolved` list. Because a commit scope is a
  branch scope, it opts out of the implicit default-branch conjunct — a hash resolving to a
  non-default branch (e.g. `release-2.1`) searches that branch, not the default.

A hash that matches no indexed branch returns empty `files` with `commit_not_indexed: true`
(never a silent unfiltered search). A prefix that collides across several branches/repos
resolves to **all** of them and scopes to the union. Search results and `get_file` carry the
resolved head's `commit` when the scope resolves to a specific branch. The `commit` tool param
on `search_code` is sugar for appending the atom. **v1 is code (`search_code`) only** —
`semantic_search` does not support `commit:` (a `commit:` in a semantic query is treated as
plain text); use `branch` there.

Whitespace means AND, `OR` (any case) means OR, and AND binds tighter:

```
a b OR c d          ==  (a AND b) OR (c AND d)
(a OR b) (c OR d)   ==  parentheses override precedence
repo:acme lang:go /Foo.*Bar/
```

### Negation

A `-` prefixing a term or group **negates** it: `-foo` excludes files matching `foo`,
`-repo:acme` excludes the `acme` repo, `-/Foo/` excludes the regex, and `-(a b)` negates a
whole group. `-` applies to any field (`-branch:x`, `-lang:go`, …). It binds to the next
term only (tighter than the implicit AND), so `-a b` is `(NOT a) AND b`; double negation is
kept as written (`--foo` is `NOT NOT foo`, not simplified). A `-` is negation only when it
starts a term with something to negate right after it — a `-` at end of input, before a
space, or before `)` (e.g. `a - b`, `foo -`, `(a -)`) stays a literal, as do interior
dashes (`foo-bar`) and dashes inside field values.

Negation on nullable columns follows standard three-valued SQL: a row with NULL `content`
matches **neither** `content:foo` nor `-content:foo`. A negated `branch:`/`commit:` is an
*exclusion*, not a scope selection, so it does **not** opt the query out of default-branch
scoping — `-branch:x foo` still searches the default branch, just excluding `x`.

> **Compatibility break.** Before this, a leading `-foo` was a literal substring. A
> bookmarked or stored `-foo` query now means negation. To search for a literal leading
> dash, quote it: `"-foo"` parses as the substring `-foo`.

Negation is lexical-search only (`search_code` and the web UI's Search page); a leading `-`
is rejected in semantic search (`semantic_search` and the web UI's Semantic page) — remove it
or quote the term to search it as text.

Unsupported syntax. Most of these raise; the first is silent and therefore more dangerous:

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
| `search_code` | `query`, `limit=200`, `branch=None`, `commit=None` | file-grouped line matches with byte ranges |
| `semantic_search` | `query`, `limit=50`, `branch=None` | ranked chunks with `rrf_score` |
| `list_repos` | — | indexed repos with per-branch last-indexed metadata |
| `get_file` | `repo`, `path`, `branch=None` | full file content, or `found: false` |
| `find_references` | `symbol`, `limit=200`, `branch=None` | ranked candidate reference (call) sites with enclosing symbols |
| `list_imports` | `repo=None`, `target=None`, `direction=imports`, `branch=None`, `limit=200` | import edge sites; `repo` required for `imports`, `target` required for `imported_by` |

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
`query_parse_error`, `query_too_broad`, `truncated`, `regex_incompatible`,
`no_content_atom`, `zero_width_only_atoms`, `commit_not_indexed` — rather than errors, so
an agent can react without a failed tool call. Pagination rides the same envelope as
`next_cursor`, and the semantic tool adds its own status fields (`semantic_enabled`,
`semantic_schema_missing`).

`semantic_search` is natural-language hybrid search (vector ANN + BM25 fused by reciprocal
rank). It is **on by default** — the `chunks` schema rides the core migration chain and
embeddings go through the workspace AI Gateway, so `make deploy` is the whole enablement
story. Each result carries the chunk's `start_line`/`end_line` (null for rows indexed
before line tracking). Opt out with `CODE_SEARCH_SEMANTIC_ENABLED=0` (on both apps and the
job); disabled, it returns `semantic_enabled: false` and touches neither the database nor
the embedder. The target Lakebase project's managed preload including
`lakebase_vector,lakebase_text` is a stated project assumption — see
[`docs/runbooks/semantic-enablement.md`](docs/runbooks/semantic-enablement.md).

`find_references` and `list_imports` serve the knowledge-graph reference edges. They are
**candidate-set, not compiler-precise** (grep-not-LSP): a call site is name-resolved to the
`symbols` definitions its callee name could plausibly mean, ranked (`same_repo`/`same_file`/
`kind_match`) but never collapsed to a single binding — `resolution` is `unique` (1
candidate), `ambiguous` (2+), or `unresolved` (0), and the true pre-cap `candidate_count`
survives capping. `list_imports` has two directions: `imports` enumerates a repo's import
sites (`repo` required) and `imported_by` finds who imports a given dotted `target`
corpus-wide (`target` required); invalid input comes back as a structured payload
(`unsupported_direction`/`missing_repo`/`missing_target` with a `reason`), never an error, and
import edges keep the full dotted path (so most read `unresolved` = external, by design).

**"What tests cover symbol X"** needs no dedicated tool: call `find_references(X)` and
client-side filter `sites` by your test-path convention (e.g. `file` starting with `tests/`);
each surviving site's `enclosing_symbol` names the covering test. Two follow-ups are
deliberately **deferred** past #87: repo/kind-scoped `find_references` filters, and per-file
forward imports ("what does file F import").

Two HTTP routes sit alongside the MCP mount: `GET /health` is liveness and never touches
the database, and `GET /ready` runs `SELECT 1 FROM repos LIMIT 1` so that a role holding
connect-but-not-select fails as 503 instead of shipping green.

## Web UI

`webui` is a second Databricks App, deployed and activated by the same
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

## Deploy

`make deploy` (see [Quick start](#quick-start)) runs `scripts/deploy.sh full`, which
deploys and activates **both** Databricks Apps in the bundle — the MCP server
(`code_search`) and the [web UI](#web-ui) (`webui`). For prod, the job run-as SP is
mandatory:

```bash
JOB_RUN_AS_SP=<client-id> make deploy-prod
```

The pipeline, in order:

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
      - your-github-username
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

### Removing a repo or branch

Removing a repo from `config.yaml`, deleting a branch upstream, narrowing a `branches:` glob,
or flipping a repo's default branch all take effect on the **next fully clean index run** — one
where every selected repo and branch resolves and indexes (or validly skips) without any
failure, conflict, or truncated branch discovery anywhere in that run. That run's post-fan-out
reconciliation checkpoint retires the dropped branch's/repo's stale rows; any failure elsewhere
in the same run leaves the whole corpus untouched rather than partially pruned (stale over
destructive). A single clean run that would purge more than half of the currently stored repos
is withheld and logged as an incident signal rather than applied — see
[`docs/runbooks/multi-branch.md` §7](docs/runbooks/multi-branch.md#7-clean-run-corpus-reconciliation)
for the full gate, the shrink guard, and how to complete a large intentional repo removal.

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

Get the URL — `make deploy` prints it at step 11, and afterwards:

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
make test                 # unit + observability; no external dependencies, no database
make test-integration     # needs an ephemeral Lakebase branch (see below)
make lint                 # ruff check + ruff format --check + mypy (incl. webui)
```

For the webui frontend specifically (requires Node):

```bash
make webui-build           # npm ci + vite build -> webui/frontend/dist/ (commit the result)
make webui-test            # vitest; advisory, not a repo gate
```

This project is **Lakebase-only**: there is no local/CI Postgres image. The integration
suite runs against an ephemeral Lakebase branch — `scripts/ci_branch.py up` (exports
`LAKEBASE_ENDPOINT`/`LAKEBASE_DATABASE`) → `scripts/migrate.py` → `make test-integration`
→ `ci_branch.py down`. CI does exactly this on every PR
([`docs/runbooks/ci-lakebase.md`](docs/runbooks/ci-lakebase.md)); the fixtures build
their own throwaway schemas, so a pre-migrated branch is fine.

`LAKEBASE_ENDPOINT` and `PGHOST` are precedence-ordered, not exclusive: a configured
`LAKEBASE_ENDPOINT` always wins; `PGHOST` selects local mode only in its absence. That
ordering matters because the deployed app's Postgres binding injects `PGHOST` at runtime,
so both are set in production.

Run the server locally with `make run` (binds `DATABRICKS_APP_PORT`, else 8000).

## Reference

- [`docs/runbooks/multi-branch.md`](docs/runbooks/multi-branch.md) — configuring and
  deploying multi-branch indexing (`branches:` globs, the 20-branch cap, `branch:`
  query semantics, the grant-coupling this migration introduces)
- [`docs/runbooks/semantic-enablement.md`](docs/runbooks/semantic-enablement.md) —
  semantic search (default-on): the preload assumption, opt-out, embeddings, memory notes
- [`docs/runbooks/indexing-parallelism.md`](docs/runbooks/indexing-parallelism.md) —
  parallel indexing: worker sizing, skip-if-unchanged, compare-and-set stamping
- [`docs/runbooks/reference-edges.md`](docs/runbooks/reference-edges.md) — the raw
  call/import edge schema (`reference_edges`, migration `0005`): what it stores, the
  no-symbol-FK design, and the grant-coupling this migration introduces
- [`docs/runbooks/ci-lakebase.md`](docs/runbooks/ci-lakebase.md) — the integration CI
  gate: ephemeral Lakebase branches, prerequisites
- [`docs/runbooks/webui.md`](docs/runbooks/webui.md) — the web UI app: auth, grants,
  rebuilding the frontend, wheel packaging
- `docs/diagrams/*.dot` — Graphviz sources for the images above. The PNGs are committed;
  edit the `.dot` and run `make diagrams` rather than touching them.
- `make help` — every target with its flags
