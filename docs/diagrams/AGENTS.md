<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-07-21 | Updated: 2026-07-21 -->

# docs/diagrams

## Purpose

Graphviz sources for the diagrams embedded in the README, each paired with a committed PNG
render. The `.dot` files are the editable source of truth; `make diagrams` renders every
`docs/diagrams/*.dot` to a sibling `.png` at 144dpi (`dot -Tpng -Gdpi=144`). The PNGs are
committed **on purpose** so GitHub renders them inline in the README — they are build
artifacts, but versioned ones. Never hand-edit a PNG.

## Key Files

| File | Description |
|------|-------------|
| `architecture.dot` | Simple data-flow chain: GitHub → indexing job → Lakebase Postgres → MCP server → MCP client |
| `architecture.png` | Committed 144dpi render of `architecture.dot` (embedded in the README intro) |
| `overall-architecture.dot` | Full system view: everything the bundle deploys and who talks to what — both apps, identities/grants, the secret scope, and the AI Gateway embedding path |
| `overall-architecture.png` | Committed 144dpi render of `overall-architecture.dot` |
| `deploy-pipeline.dot` | The 11 steps of `scripts/deploy.sh full`, including the split migrate/grant steps and the dashed first-activation fallback branches for each app |
| `deploy-pipeline.png` | Committed 144dpi render of `deploy-pipeline.dot` |

## Subdirectories

None.

## For AI Agents

### Working In This Directory

- **Workflow:** edit the `.dot`, run `make diagrams` (requires graphviz's `dot` on PATH),
  and commit **both** the `.dot` and the regenerated `.png` in the same change. Never edit
  a PNG directly, and never commit a `.dot` change without its re-rendered PNG.
- **Style conventions** (consistent across all three sources — keep new diagrams
  matching):
  - `fontname="Helvetica"` on nodes and edges; nodes are `shape=box,
    style="rounded,filled"`.
  - Databricks-side components: `fillcolor="#e6f0fa"`, `color="#2b6cb0"`. External
    components (GitHub, clients, config): `fillcolor="#edf2f7"` with the default
    `#4a5568` border.
  - `rankdir=TB` (vertical) on purpose: a left-to-right chain renders ~2300px wide and
    GitHub downscales it until labels are unreadable. Do not flip to LR.
  - Each `.dot` opens with a comment block stating its scope and the "Regenerate with
    `make diagrams` (do not hand-edit the PNG)" reminder — keep that header.
  - `deploy-pipeline.dot` additionally uses `#fffaf0`/`#c05621` for the
    migrate/activate/grant steps, red `shape=note` fallback nodes with dashed edges, and
    an edge `weight=100` to pin the main spine straight.
- The README embeds the PNGs by exact path with `width` attributes and descriptive `alt`
  text — if a diagram's content changes materially, update the corresponding `alt` text
  in `README.md` too. Renaming a `.dot`/`.png` pair breaks the README embed.

### Testing Requirements

Nothing beyond render + link integrity: `make diagrams` must succeed cleanly, and the
README's `<img src="docs/diagrams/*.png">` paths must still resolve. There is no automated
check that a PNG is in sync with its `.dot` — regenerating on every `.dot` edit is the
discipline.

### Common Patterns

- One diagram per concern; overlap is handled by cross-referencing in the header comment
  (`overall-architecture.dot` explicitly says the simple chain lives in
  `architecture.dot`) rather than duplicating nodes.
- Edge labels carry the operational detail (identity, grant, transport, cadence) so the
  diagram stands alone without a legend.

## Dependencies

Graphviz — the `dot` binary must be on PATH for `make diagrams` (the target fails loudly
if it is missing). Nothing else.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
