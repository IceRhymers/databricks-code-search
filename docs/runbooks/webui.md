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

## Branch-scoped permalinks (issue #46)

Lexical (`/api/search`) results carry a service-selected `permalink_branch` per file entry
(`null` when the query had no `branch:` filter, otherwise the branch that resolved the file's
matched content version). The Search page threads it into every file/line link it renders, so
opening a result from a `branch:`-filtered search opens that branch's version of the file
rather than falling back to the repo's default branch. Semantic results are unaffected --
`ChunkCard` links never carry a branch.

`GET /api/file` accepts an optional `branch` query param; omitted, it keeps its existing
default-branch resolution unchanged.

## Branch filter chips (issue #47)

The Search page's `repo:`/`lang:` chips (`FilterChips.tsx`) compose atoms into the query
string via a small client-side recognizer (`utils/queryModel.ts`) that classifies the typed
query as either a flat AND of `field:value`/bareword atoms ("safe" -- the chips can add,
remove, or replace an atom without touching anything else) or "unsafe" (contains `OR`,
parens, a quoted string, or a `/regex/` -- any construct a naive atom rewrite could silently
corrupt). The recognizer is a deliberately narrower single-pass port of
`app/query/parser.py:tokenize`, not a full parser; `tests/unit/test_query_corpus_parity.py`
and `webui/frontend/src/utils/queryModel.corpus.test.ts` share one JSON corpus
(`queryModel.corpus.json`) to keep the TS port's "safe" verdict from drifting out of sync
with what the real backend parser accepts.

A `branch:name` chip row renders per repository, but only when the query names **exactly
one** known repository (an exact `repo:` atom match, never a regex/glob repo pattern, and
never a union across repos) and has at most one existing `branch:` atom -- deliberately
narrower than the repo/lang chips, which stay visible (disabled, with a tooltip) for any
unsafe query. This asymmetry exists because a branch chip's option list depends on knowing
which single repo it applies to; repo/lang toggles do not have that ambiguity. See
`deriveChips` in `queryModel.ts` for the exact rules.

## Semantic search (issue #36)

The webui exposes the same hybrid semantic engine (`app/search/semantic.py`) the MCP
`semantic_search` tool uses, via two routes on the same FastAPI app, and a Semantic tab in
the SPA. Both routes are thin passthrough wrappers over `app.service` payload builders --
they never reimplement search logic, and MCP responses are unaffected.

### `GET /api/semantic/status`

Zero-DB, zero-SDK: reads `cfg.semantic_enabled` straight off this app's own `Settings` and
returns `{"semantic_enabled": <bool>}`. This drives the SPA's nav-link visibility --
`App.tsx` fetches it once at mount and renders the "Semantic" nav link only when true. A
failed fetch (network error, etc.) leaves it `false` -- the nav link fails **closed**, not
open. Note this reflects the **webui app's own** flag; the MCP app has a separate `app.yaml`
and can have the flag set independently (see
[`docs/runbooks/semantic-enablement.md`](semantic-enablement.md) section 2(d)) -- the tab
stays hidden until the webui app itself has `CODE_SEARCH_SEMANTIC_ENABLED=1`.

### `GET /api/semantic`

Params: `q` (required, non-empty), `limit` (default `50` -- parity with the MCP
`semantic_search` tool's own default, not `/api/search`'s `0 -> row_limit` convention),
`branch` (optional, threads straight to the SQL predicate as-is, never appended to a query
string the way `/api/search`'s `branch:` sugar works).

The response is the builder's payload passed through unchanged (`clamp_limit` still applies
to `limit`):
- **Disabled** (`semantic_enabled: false` + `reason`) and **not-migrated**
  (`semantic_schema_missing: true` + `reason`) are 200 bodies -- recoverable conditions are
  payload fields, never HTTP errors, mirroring the MCP tool's dispatch contract.
- **Enabled + migrated**: 200 with `results` (chunk-level `{repo, file, chunk_index,
  content, rrf_score}`, in RRF-descending/id-tiebreak order -- see the V1 limitation below),
  `backend`, and `count`.
- **400** `{"error": "invalid parameter"}` -- a NUL byte in `q`/`branch` reaching a bound SQL
  parameter (`sqlalchemy.exc.DataError`).
- **422** -- FastAPI request validation (missing/empty `q`), parity with `/api/search`.
- **502** `{"error": "semantic search backend unavailable"}` -- any other exception (embedding
  endpoint auth/network failures are arbitrary exception types). The raw error is logged
  server-side (`logger.exception`) and never echoed in the response body, mirroring `/ready`'s
  no-leak policy.

All error bodies use the same `{"detail": {"error": "..."}}` wire shape FastAPI produces for
`HTTPException(detail={"error": ...})`, identical to `/api/search`/`/api/file`.

### The Semantic tab

A separate `/semantic` route and page (`SemanticPage.tsx`), not a mode toggle on the search
page -- semantic results are a structurally different envelope (top-k ranked chunks) from
grep's cursor-paginated line matches, and the two modes answer different questions
(natural-language relevance vs. pattern matching). Consequences of that separation:

- **No fusion with grep.** Semantic and grep results never interleave in one list; there is
  no cross-mode dedup pass.
- **Flat RRF order.** Chunk cards render in exactly the payload's order (RRF score
  descending, `id` tiebreak) -- the UI never re-sorts. The same file can appear as multiple
  cards (one per matching chunk) since chunks, not files, are the ranked unit.
- **Per-chunk cards** (`ChunkCard.tsx`): each shows `{repo}/{file}`, the chunk index, the RRF
  score (`rrf_score.toFixed(4)`), and the chunk's raw content (no syntax highlighting --
  highlighting every result chunk is FilePage's cost profile, not a results list's).
- The route stays registered even when the nav tab is hidden: a direct `/semantic` deep link
  renders the page's own explanatory state (disabled banner or not-migrated banner) rather
  than a 404.

### Best-effort "open at nearest line" anchor

`chunks` carries no line ranges (a V1 limitation of the schema -- see below), so clicking a
`ChunkCard` opens `/file?repo=...&path=...&find=<needle>` where `find` is the chunk's
**longest non-empty trimmed line** (`extractNeedle` in `utils/chunkAnchor.ts` -- deliberately
not the first line, since the first line of a token-cut chunk tends to be generic and causes
silent wrong-line hits). `FilePage` then re-locates that exact text in the file content it
already fetched (`locateNeedleLine`) and rewrites the URL to the usual `#L<n>` anchor, so the
existing scroll/highlight behavior takes over unchanged. Three outcomes:
- **Unique hit:** rewrites straight to `#L<n>`.
- **Multiple occurrences:** still jumps to the first occurrence, plus a dismissible note that
  the line appears more than once and the jump may not be the chunk's exact location.
- **Miss** (content changed shape since indexing, or was re-indexed): opens the file at the
  top with a dismissible note that the chunk couldn't be located.

### V1 limitation: no index-time line ranges

The `chunks` table stores content only, not `start_line`/`end_line` -- chunk boundaries come
from token-cutting, not the file's line structure, so there is no authoritative line anchor
to render. The client-side needle match above is the interim behavior; adding real line
ranges would require a gated migration revision on an already-shipped table, a chunk-writer
change, and a full re-index of every semantically-enabled project, so it is out of scope here
and tracked as a follow-up issue (index-time `start_line`/`end_line` for exact semantic
anchors).

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

## Security headers

Every response (API routes and the SPA alike) carries `X-Content-Type-Options: nosniff` and
`X-Frame-Options: DENY` (`webui/main.py`'s `_add_security_headers` middleware). A
Content-Security-Policy is deliberately NOT set yet: Shiki's syntax highlighting
(`webui/frontend/src/components/CodeBlock.tsx`) emits inline per-token `style="color:..."`
attributes, which a strict CSP would need `style-src 'unsafe-inline'` (or a nonce/hash scheme
threaded through Shiki's token rendering) to allow without breaking the file view. Tracked as
open follow-up work — add a CSP once that inline-style path is addressed.

## Rebuilding the frontend

The production frontend build (`webui/frontend/dist/`) is **committed** — CI and deploy do
not need Node. To rebuild it after a frontend change:

```bash
make webui-build   # cd webui/frontend && npm ci && npm run build
```

Commit the resulting `webui/frontend/dist/` changes. `npm test` (vitest) is available via
`make webui-test` and is enforced by CI's `webui` job (which runs `npm ci` first); it is not
wired into `make test` / `make test-integration`.

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
