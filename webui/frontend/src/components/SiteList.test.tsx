// vitest runs with environment: "node" (vite.config.ts) -- no jsdom, no @testing-library.
// SiteList.tsx has no module-scope window reads (unlike router.ts-importing pages), so no
// vi.stubGlobal("window") stub is needed here, unlike GraphPage.test.tsx / router.test.ts.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ReferenceSite } from "../api/client";
import { SiteList } from "./SiteList";

function site(overrides: Partial<ReferenceSite> = {}): ReferenceSite {
  return {
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
    ...overrides,
  };
}

const summary = { unique: 0, ambiguous: 1, unresolved: 0 };

describe("SiteList", () => {
  it("renders 'module scope' when enclosing_symbol is null", () => {
    const markup = renderToStaticMarkup(
      <SiteList
        sites={[site({ enclosing_symbol: null })]}
        siteCount={1}
        resolutionSummary={summary}
        truncated={false}
        truncationReason={null}
        branch={null}
        emptyMessage="No sites."
      />
    );
    expect(markup).toContain("module scope");
  });

  it("renders the resolution badge", () => {
    const markup = renderToStaticMarkup(
      <SiteList
        sites={[site({ resolution: "unique" })]}
        siteCount={1}
        resolutionSummary={{ unique: 1, ambiguous: 0, unresolved: 0 }}
        truncated={false}
        truncationReason={null}
        branch={null}
        emptyMessage="No sites."
      />
    );
    expect(markup).toContain("resolution-unique");
    expect(markup).toContain(">unique<");
  });

  it("renders same_repo/kind_match signal chips per candidate but not same_file when false", () => {
    const markup = renderToStaticMarkup(
      <SiteList
        sites={[
          site({
            candidates: [
              {
                repo: "acme/widgets",
                file: "src/service.py",
                line: 10,
                name: "process",
                kind: "function",
                same_repo: true,
                same_file: false,
                kind_match: true,
              },
            ],
          }),
        ]}
        siteCount={1}
        resolutionSummary={summary}
        truncated={false}
        truncationReason={null}
        branch={null}
        emptyMessage="No sites."
      />
    );
    expect(markup).toContain("same repo");
    expect(markup).toContain("kind match");
    expect(markup).not.toContain("same file");
  });

  it("renders a 'showing N of M' note when candidates_truncated", () => {
    const markup = renderToStaticMarkup(
      <SiteList
        sites={[
          site({
            candidate_count: 5,
            candidates_truncated: true,
            candidates: [
              {
                repo: "acme/widgets",
                file: "src/service.py",
                line: 10,
                name: "process",
                kind: "function",
                same_repo: true,
                same_file: true,
                kind_match: true,
              },
            ],
          }),
        ]}
        siteCount={1}
        resolutionSummary={summary}
        truncated={false}
        truncationReason={null}
        branch={null}
        emptyMessage="No sites."
      />
    );
    expect(markup).toContain("showing 1 of 5");
  });

  it("deep-links a site and its candidates via the fileHref idiom, branch threaded", () => {
    const markup = renderToStaticMarkup(
      <SiteList
        sites={[
          site({
            candidates: [
              {
                repo: "acme/widgets",
                file: "src/service.py",
                line: 10,
                name: "process",
                kind: "function",
                same_repo: true,
                same_file: false,
                kind_match: true,
              },
            ],
          }),
        ]}
        siteCount={1}
        resolutionSummary={summary}
        truncated={false}
        truncationReason={null}
        branch="feature/x"
        emptyMessage="No sites."
      />
    );
    expect(markup).toContain(
      "/file?repo=acme%2Fwidgets&amp;path=src%2Fcaller.py&amp;branch=feature%2Fx#L5"
    );
    expect(markup).toContain(
      "/file?repo=acme%2Fwidgets&amp;path=src%2Fservice.py&amp;branch=feature%2Fx#L10"
    );
  });

  it("shows the emptyMessage when there are no sites, and the row-cap truncation banner", () => {
    const markup = renderToStaticMarkup(
      <SiteList
        sites={[]}
        siteCount={0}
        resolutionSummary={{ unique: 0, ambiguous: 0, unresolved: 0 }}
        truncated={true}
        truncationReason="row_cap"
        branch={null}
        emptyMessage="No reference sites."
      />
    );
    expect(markup).toContain("No reference sites.");
    expect(markup).toContain("row_cap");
  });
});
