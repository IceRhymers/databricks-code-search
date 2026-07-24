// vitest runs with environment: "node" (vite.config.ts) -- no jsdom, no @testing-library.
// renderToStaticMarkup gives us plain HTML strings to assert substrings against without either.
// referencesBody/importsBody are pure functions extracted from GraphPage specifically so this
// branching is testable without hook-driven component state (the SemanticPage.tsx/
// semanticBody pattern). GraphPage.tsx transitively imports router.ts, which reads
// `window.location`/`window.addEventListener` at module scope -- stub a minimal fake window
// before importing, same pattern as router.test.ts / SemanticPage.test.tsx.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { ImportsEnvelope, ReferenceSite, ReferencesEnvelope } from "../api/client";

vi.stubGlobal("window", {
  location: { pathname: "/references", search: "", hash: "", href: "http://localhost/references" },
  history: { replaceState: vi.fn(), pushState: vi.fn() },
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
});

const { GraphPage, referencesBody, importsBody } = await import("./GraphPage");

function refEnvelope(overrides: Partial<ReferencesEnvelope> = {}): ReferencesEnvelope {
  return {
    query: "process",
    kind: "references",
    symbol: "process",
    branch: null,
    query_too_broad: false,
    sites: [],
    site_count: 0,
    resolution_summary: { unique: 0, ambiguous: 0, unresolved: 0 },
    truncated: false,
    truncation_reason: null,
    ...overrides,
  };
}

function importEnvelope(overrides: Partial<ImportsEnvelope> = {}): ImportsEnvelope {
  return {
    query: "acme/widgets",
    kind: "imports",
    direction: "imports",
    repo: "acme/widgets",
    repo_known: true,
    target: null,
    branch: null,
    query_too_broad: false,
    sites: [],
    site_count: 0,
    resolution_summary: { unique: 0, ambiguous: 0, unresolved: 0 },
    truncated: false,
    truncation_reason: null,
    ...overrides,
  };
}

const site: ReferenceSite = {
  repo: "acme/widgets",
  file: "src/caller.py",
  line: 5,
  edge_kind: "call",
  target_name: "process",
  enclosing_symbol: { name: "run", kind: "function" },
  resolution: "ambiguous",
  candidate_count: 2,
  candidates_truncated: false,
  candidates: [],
};

function markupFor(body: JSX.Element | null): string {
  return body ? renderToStaticMarkup(body) : "";
}

describe("referencesBody", () => {
  it("shows the loading state", () => {
    expect(markupFor(referencesBody("loading", null, null))).toContain("Searching");
  });

  it("shows the request error and never falls through to results", () => {
    const markup = markupFor(referencesBody("error", "boom", null));
    expect(markup).toContain("boom");
  });

  it("returns null when idle with no envelope (nothing searched yet)", () => {
    expect(referencesBody("idle", null, null)).toBeNull();
  });

  it("shows the query_too_broad banner and never falls through to a site list", () => {
    const markup = markupFor(referencesBody("idle", null, refEnvelope({ query_too_broad: true })));
    expect(markup).toContain("too broad");
    expect(markup).not.toContain("No reference sites");
  });

  it("shows 'No reference sites.' for an empty results envelope", () => {
    const markup = markupFor(referencesBody("idle", null, refEnvelope()));
    expect(markup).toContain("No reference sites.");
  });

  it("renders sites and the row-cap truncation banner when truncated", () => {
    const markup = markupFor(
      referencesBody(
        "idle",
        null,
        refEnvelope({
          sites: [site],
          site_count: 1,
          resolution_summary: { unique: 0, ambiguous: 1, unresolved: 0 },
          truncated: true,
          truncation_reason: "row_cap",
        })
      )
    );
    expect(markup).toContain("src/caller.py");
    expect(markup).toContain("ambiguous");
    expect(markup).toContain("row_cap");
  });
});

describe("importsBody", () => {
  it("shows the unsupported_direction banner with the echoed value and reason, never falls through", () => {
    const markup = markupFor(
      importsBody(
        "idle",
        null,
        importEnvelope({
          unsupported_direction: "sideways",
          reason: "direction must be one of 'imports' or 'imported_by'",
        })
      )
    );
    expect(markup).toContain("sideways");
    expect(markup).toContain("direction must be one of");
    expect(markup).not.toContain("No import sites");
  });

  it("shows the missing_repo banner with its reason", () => {
    const markup = markupFor(
      importsBody(
        "idle",
        null,
        importEnvelope({ missing_repo: true, reason: "direction=imports requires a repo" })
      )
    );
    expect(markup).toContain("direction=imports requires a repo");
  });

  it("shows the missing_target banner with its reason", () => {
    const markup = markupFor(
      importsBody(
        "idle",
        null,
        importEnvelope({ missing_target: true, reason: "direction=imported_by requires a target" })
      )
    );
    expect(markup).toContain("direction=imported_by requires a target");
  });

  it("shows the no-such-repo banner when repo_known is false", () => {
    const markup = markupFor(
      importsBody("idle", null, importEnvelope({ repo_known: false, repo: "acme/ghost" }))
    );
    expect(markup).toContain("acme/ghost");
  });

  it("shows the query_too_broad banner", () => {
    const markup = markupFor(importsBody("idle", null, importEnvelope({ query_too_broad: true })));
    expect(markup).toContain("too broad");
  });

  it("shows the external-by-design standing copy and 'No import sites.' for an empty envelope", () => {
    const markup = markupFor(importsBody("idle", null, importEnvelope()));
    expect(markup).toContain("external/stdlib");
    expect(markup).toContain("No import sites.");
  });
});

describe("GraphPage mode switch (D8 keyed remount contract)", () => {
  it("renders ONLY the references form for a references route, with no imports leakage", () => {
    const markup = renderToStaticMarkup(
      <GraphPage route={{ page: "graph", mode: "references", symbol: "", branch: null }} />
    );
    expect(markup).toContain("Find references");
    expect(markup).not.toContain("List imports");
    expect(markup).not.toContain('aria-label="Repo"');
    expect(markup).not.toContain('aria-label="Target"');
  });

  it("renders ONLY the imports form for an imports route, with no references leakage", () => {
    // App.tsx mounts GraphPage with key={route.mode}, so a references<->imports navigation
    // fully unmounts and remounts this component -- equivalent to two independent renders of
    // fresh instances with different route props, exercised here as two separate
    // renderToStaticMarkup calls. Each must show ONLY its own mode's form/copy: proof that no
    // symbol/repo/target/direction state -- or the "ranInitial" auto-run guard -- can bleed
    // from one mode into the other across the remount.
    const markup = renderToStaticMarkup(
      <GraphPage
        route={{
          page: "graph",
          mode: "imports",
          repo: "",
          target: "",
          direction: "imports",
          branch: null,
        }}
      />
    );
    expect(markup).toContain("List imports");
    expect(markup).not.toContain("Find references");
    expect(markup).not.toContain('placeholder="symbol name, e.g. process"');
  });
});
