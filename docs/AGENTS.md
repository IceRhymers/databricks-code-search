<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# docs

## Purpose

Operator-facing documentation that does not belong in the README. This directory holds no
prose of its own — it is a container for two subdirectories: `runbooks/` (deep operational
runbooks the README links into at specific anchors) and `diagrams/` (Graphviz `.dot`
sources plus their committed PNG renders, embedded in the README). The README remains the
entry point; files here carry the detail — deploy coupling, failure modes, 2am recovery
procedures, and the rendered architecture/deploy diagrams.

## Key Files

| File | Description |
|------|-------------|
| `runbooks/AGENTS.md` | Guide to the operational runbooks subdirectory |
| `diagrams/AGENTS.md` | Guide to the Graphviz diagram sources and committed PNGs |

## Subdirectories

- [`runbooks/`](runbooks/AGENTS.md) — operational runbooks (multi-branch indexing,
  semantic enablement, CI-on-Lakebase, web UI, indexing parallelism). See
  `runbooks/AGENTS.md`.
- [`diagrams/`](diagrams/AGENTS.md) — three `.dot` sources with committed PNGs rendered
  by `make diagrams`. See `diagrams/AGENTS.md`.

## For AI Agents

### Working In This Directory

- The README links into these files at **specific paths and heading anchors** (e.g.
  `docs/runbooks/multi-branch.md#4-branch-query-semantics--exact-not-a-glob` and the three
  `docs/diagrams/*.png` `<img>` embeds). Renaming a file, a heading, or a PNG breaks the
  README silently — check `README.md` for inbound links before any rename.
- Diagram PNGs are build artifacts of `make diagrams` but are **committed on purpose** so
  GitHub renders them. Never hand-edit a PNG; edit the `.dot` and regenerate.
- Runbooks are prose, not generated — edit them directly, keeping their anchors stable.

### Testing Requirements

No test suite covers this directory beyond link/anchor integrity. After edits, verify that
README links and cross-runbook links still resolve; that is the whole obligation.

### Common Patterns

- Runbooks state deploy coupling explicitly ("this migration is NOT schema-only") and end
  with cross-references to related runbooks and plans.
- Diagrams and runbooks cross-link rather than duplicate: the README carries the summary,
  the runbook carries the operational depth, the diagram carries the picture.

## Dependencies

- `diagrams/`: Graphviz (`dot` on PATH) for `make diagrams`.
- `runbooks/`: none — plain Markdown.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
