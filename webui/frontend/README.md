# webui frontend

Vite + React + TypeScript SPA for the code-search web UI. Talks to the
FastAPI backend in `webui/main.py` via `/api/*`.

## Develop

```sh
npm install
npm run dev       # Vite dev server; proxies /api to http://127.0.0.1:8000 (run the backend separately)
```

## Build

```sh
npm run build      # tsc -b && vite build -> dist/
```

`dist/` is committed to the repo (see the root `.gitignore` negation) because DABs source sync
respects `.gitignore`, and production deploy does not require Node — `webui/main.py` serves
this directory directly via `SPAStaticFiles`. **Rebuild and commit `dist/` whenever `src/`
changes**; CI's `webui` job runs a Node build step (`make webui-verify-dist`) purely to enforce
that the committed `dist/` matches `src/`, not to produce a deploy artifact.

## Test

```sh
npm test           # vitest run
```

Covers the two pieces of app logic with real correctness risk: the UTF-8 byte-range highlight
splitter (`src/utils/byteRanges.ts`, exercised with multi-byte/emoji lines) and the
paginated-search reducer (`src/utils/searchReducer.ts`, cursor-append / banner-per-page
semantics), plus the flat-AND query recognizer (`src/utils/queryModel.ts`) that drives the
repo/lang/branch filter chips, whose `queryModel.corpus.json` corpus is also asserted against
the real Python parser by `tests/unit/test_query_corpus_parity.py`. The vitest suite runs in
CI's `webui` job (`make webui-test`); the Python half of the corpus parity check is a
`pytest -m unit` test and runs under `make test` in the `unit` job.

## Notable choices

- No router/state-management dependency: three pages, hand-rolled via `useSyncExternalStore`
  (`src/router.ts`, `src/theme.ts`).
- Syntax highlighting (`src/components/CodeBlock.tsx`) uses Shiki's fine-grained `shiki/core`
  API with static per-language imports for exactly the 7 languages the indexer recognizes
  (`indexer/languages.py`), plus the JS regex engine (no wasm binary to ship/stream). Importing
  the top-level `shiki` package instead would pull in its full ~80-language bundle, since Vite
  can't tree-shake a runtime `bundledLanguages[key]` lookup.
