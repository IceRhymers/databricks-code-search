<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# webui/frontend

## Purpose
Vite + React 18 + TypeScript SPA for the code-search web UI (issue #35). Talks to the FastAPI backend (`webui/main.py`) exclusively via same-origin `/api/*` GETs; in production the backend serves the committed `dist/` build through `SPAStaticFiles` on the same origin, while `npm run dev` proxies `/api` to `http://127.0.0.1:8000` (see `vite.config.ts`). Dependencies are deliberately lean: `react`, `react-dom`, and `shiki` are the only runtime deps — routing and theming are hand-rolled with `useSyncExternalStore` instead of a router/state library.

## Key Files
| File | Description |
|------|-------------|
| `package.json` | Scripts: `dev` (Vite + proxy), `build` (`tsc -b && vite build` → `dist/`), `test` (`vitest run`). Runtime deps: react, react-dom, shiki only. |
| `vite.config.ts` | Single config for Vite AND vitest (`vitest/config`): build to `dist/` with `emptyOutDir`, dev proxy for `/api`, test environment `"node"` matching `src/**/*.test.{ts,tsx}`. |
| `tsconfig.json` / `tsconfig.node.json` | Strict TS project references (ES2022, `moduleResolution: "Bundler"`, `noEmit`, `noUnusedLocals`/`Parameters`); `tsc -b` runs as part of the build, so type errors fail `make webui-build`. |
| `index.html` | SPA shell entry loading `src/main.tsx`. |
| `dist/` | The COMMITTED production build (`index.html` + hashed `assets/*`). Served directly by `webui/main.py`; committed because DABs source sync respects `.gitignore` and CI/deploy have no Node step (root `.gitignore` has an explicit `!webui/frontend/dist/` negation). |
| `README.md` | Human-facing develop/build/test notes and the rationale for the committed `dist/` and Shiki fine-grained imports. |

## Subdirectories
| Directory | Description |
|-----------|-------------|
| `src/` | All application source (pages, components, utils, api client, router, theme) — see `src/AGENTS.md`. |
| `dist/` | Committed build output — never hand-edit (no AGENTS.md; regenerate via `make webui-build`). |

## For AI Agents

### Working In This Directory
- **`dist/` is committed build output — never hand-edit it.** After any `src/` (or config) change, run `make webui-build` (`npm ci && npm run build`) and commit the resulting `dist/` diff. There is no build-time check that `dist/` and `src/` are in sync; a stale `dist/` ships silently.
- The build must stay self-contained and same-origin: no absolute API base URLs (the client uses relative `/api/...` paths), no new runtime CDN/network dependencies.
- Keep the dependency budget: prefer hand-rolled solutions (`router.ts`, `theme.ts`) over adding a router/state/fetch library. Shiki must stay on the fine-grained `shiki/core` API with static per-language imports — importing top-level `shiki` pulls the full ~80-language bundle because Vite cannot tree-shake a runtime `bundledLanguages[key]` lookup.
- Deep links work because the backend falls back to `index.html` for non-`/api` 404s; new client routes need a matching case in `src/router.ts`, nothing server-side.

### Testing Requirements
- `make webui-test` (= `npm test`, `vitest run`) — runs in CI's `webui` job (after `npm ci`) but is NOT wired into `make test`/`make test-integration`; advisory for local Python-only workflows, enforced in CI.
- Tests run in a plain `node` environment: no jsdom, no @testing-library. Component tests assert substrings against `renderToStaticMarkup` output; DOM-flavored logic is factored into pure functions and tested directly.
- `src/utils/queryModel.corpus.json` is shared with `tests/unit/test_query_corpus_parity.py` (pytest, `make test`) — changing recognizer behavior requires updating the corpus, and both suites must pass.
- `tsc -b` is part of `npm run build`, so the strict TS config is effectively a second lint gate.

### Common Patterns
- Pure logic lives in `src/utils/` with colocated `*.test.ts`; components stay thin over it.
- Wire types have exactly one home (`src/utils/searchReducer.ts` for search, `src/api/client.ts` for the rest); the api client only knows how to reach routes.
- File-top block comments explain the "why" (backend contract references, issue numbers) — preserve them.

## Dependencies

### Internal
- `webui/main.py` (serves `dist/`, defines the `/api/*` contract); `indexer/languages.py` (the 7 highlighted languages mirror `EXT_TO_LANG`); `app/query/parser.py` (source of truth `queryModel.ts` mirrors).

### External
- Runtime: `react` ^18.3, `react-dom` ^18.3, `shiki` ^1.24. Dev: `vite` ^5, `vitest` ^2, `typescript` ^5.6, `@vitejs/plugin-react`. Node/npm needed only for local dev and rebuilds — never for CI deploy or the Apps runtime.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
