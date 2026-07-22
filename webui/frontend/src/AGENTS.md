<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# webui/frontend/src

## Purpose
Application source for the SPA: a hand-rolled router and theme store (both `useSyncExternalStore`-based, no library), four pages (Search, File, Repos, Semantic), presentational components, and a `utils/` layer holding all the logic with real correctness risk — the UTF-8 byte-range highlight splitter, the paginated-search reducer, the flat-AND query recognizer behind the filter chips, and the chunk-anchor needle matcher. The Semantic nav tab is fail-closed: `App.tsx` probes `/api/semantic/status` once at mount and shows the tab only on a confirmed `true` (a failed fetch keeps it hidden), while the `/semantic` route itself stays registered so deep links render an explanatory state instead of a 404.

## Key Files
| File | Description |
|------|-------------|
| `main.tsx` | React entry point mounting `App`. |
| `App.tsx` | Shell: nav header, theme toggle, route switch, fail-closed semantic-tab probe. |
| `router.ts` | Minimal client-side router: `Route` union for the four pages, `parseLocation` (including `#L12` / `#L12-L24` line-anchor parsing), link interception, `replaceRoute`; hydrates from `location` because the backend serves `index.html` for any non-API deep link. |
| `theme.ts` | Light/dark store: OS `prefers-color-scheme` default, localStorage override, `data-theme` attribute on `<html>`. |
| `index.css` | All styling; keys off both `prefers-color-scheme` (pre-hydration first paint) and the explicit `data-theme` attribute. |
| `router.test.ts` | Zero-dependency DOM-free router tests. |

## Subdirectories
| Directory | Description |
|-----------|-------------|
| `api/` | `client.ts` — thin typed `fetch` wrappers for every backend route (`searchCode`, `getFile`, `listRepos`, `getSemanticStatus`, `semanticSearch`) plus `ApiError` mapping the backend's `{error: string}` bodies; wire types for non-search envelopes live here. `SemanticResult.similarity` (`1 - cosine_distance`, null for pre-embedding rows) and `SemanticEnvelope`'s three mutually-exclusive optional error fields (`query_parse_error`, `unsupported_filter`, `nothing_to_embed`, each pairing with the existing `reason`) mirror the in-query filter-grammar contract on `/api/semantic`. |
| `components/` | Presentational pieces: `ResultsList` (grep hits with byte-range highlights), `Banner` (envelope-field-driven banners only — never inferred from empty results), `FilterChips` (repo/lang/branch chips), `ChunkCard` (semantic results; exact `#L` anchor when `start_line`/`end_line` present, needle fallback otherwise; renders `similarity` beside the rrf score, null-safe), `CodeBlock` (Shiki `shiki/core` with static imports for exactly the 7 indexed languages), `SyntaxHelp`, `ThemeToggle`. |
| `pages/` | Route-level containers: `SearchPage` (reducer-driven cursor pagination + chips), `FilePage` (full-file view, line-anchor scroll/highlight, needle relocation via `locateNeedleLine`), `ReposPage` (indexed-repo listing), `SemanticPage` (RRF-ordered chunk cards, disabled/not-migrated/filter-grammar-error in-tab states via the exported pure `semanticBody`; the query box accepts in-query `repo:`/`file:`/`lang:` scoping, not `FilterChips` — the semantic tab doesn't compose queries). |
| `utils/` | The tested logic core: `byteRanges.ts` (UTF-8 byte offsets → UTF-16-safe highlight segments via TextEncoder/TextDecoder), `searchReducer.ts` (cursor-append pagination; banners reflect only the most recent page), `queryModel.ts` (single-pass safe/unsafe query recognizer mirroring `app/query/parser.py:tokenize`, plus `deriveChips`; parity-locked by `queryModel.corpus.json`), `chunkAnchor.ts` (`extractNeedle` — longest non-empty trimmed line — and `locateNeedleLine`). |

## For AI Agents

### Working In This Directory
- After any change here, rebuild the committed `dist/` with `make webui-build` and commit it — the backend serves `dist/`, not this source.
- **Contract-mirroring modules must not drift.** `queryModel.ts` mirrors `app/query/parser.py`; behavior changes require updating `queryModel.corpus.json`, which `tests/unit/test_query_corpus_parity.py` asserts against the real Python parser. `byteRanges.ts` depends on the backend emitting UTF-8 byte offsets (`app/search/grep.py`). `CodeBlock.tsx`'s language list mirrors `indexer/languages.py:EXT_TO_LANG`.
- Banners come ONLY from envelope fields (`truncated`, `query_too_broad`, `regex_incompatible`, `resolved`, ...) — never inferred from an empty `files` list; new signals are additive envelope fields.
- Semantic results render in exactly the payload's RRF order — never re-sort, never fuse/dedup with grep results.
- Preserve the pagination contract: `cursor` is always sent explicitly, `next_cursor !== null` drives "Load more", and a continuation page can clear a banner a prior page raised.
- No new runtime dependencies without strong justification; keep logic in pure `utils/` functions so it stays testable without a DOM.

### Testing Requirements
- `make webui-test` (vitest, node environment — no jsdom): colocated `*.test.ts(x)` files. Component tests use `renderToStaticMarkup` + substring assertions.
- Highest-risk areas carry the tests: `byteRanges` (multi-byte/emoji lines), `searchReducer` (cursor-append / banner-per-page), `queryModel` (+ corpus parity, whose Python half runs under `make test`), `chunkAnchor`, `router`.
- Advisory locally, enforced by CI's `webui` job; `tsc -b` (strict) also gates the build.

### Common Patterns
- Pages own fetch + state (discriminated-union `useState` or `useReducer`); components are props-only and stateless where possible.
- Errors surface via `ApiError` (`status` + backend `error` message) — catch it per page, render inline states.
- Exported pure helpers (`chunkHref`, `extractNeedle`, `deriveChips`, `recognize`) are the unit-test surface; keep new logic in that shape.

## Dependencies

### Internal
- Backend route contract in `webui/main.py` / `app.service` payload shapes; parser/grep/indexer mirrors noted above; `docs/runbooks/webui.md` documents the user-facing behavior these modules implement.

### External
- `react` / `react-dom` (incl. `react-dom/server` in tests), `shiki/core` + per-language/theme fine-grained imports, `vitest`. Browser APIs: `fetch`, `URLSearchParams`, `TextEncoder`/`TextDecoder`, `matchMedia`, `localStorage`.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
