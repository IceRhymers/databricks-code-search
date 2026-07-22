// vitest runs with environment: "node" (vite.config.ts) -- no jsdom, no @testing-library.
// renderToStaticMarkup gives us plain HTML strings to assert substrings against without either.
// semanticBody is a pure function extracted from SemanticPage specifically so this branching
// (loading/error/disabled/not-migrated/filter-errors/results) is testable without hook-driven
// component state. SemanticPage.tsx transitively imports router.ts, which reads
// `window.location`/`window.addEventListener` at module scope (see router.test.ts) -- stub a
// minimal fake window before importing, same pattern as router.test.ts.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { SemanticEnvelope } from "../api/client";

vi.stubGlobal("window", {
  location: { pathname: "/semantic", search: "", hash: "", href: "http://localhost/semantic" },
  history: { replaceState: vi.fn(), pushState: vi.fn() },
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
});

const { semanticBody } = await import("./SemanticPage");

function envelope(overrides: Partial<SemanticEnvelope> = {}): SemanticEnvelope {
  return {
    query: "how are branch filters compiled to SQL",
    semantic_enabled: true,
    results: [],
    count: 0,
    ...overrides,
  };
}

function markupFor(env: SemanticEnvelope | null, enabled: boolean | null = null): string {
  const body = semanticBody("idle", null, env, enabled);
  return body ? renderToStaticMarkup(body) : "";
}

describe("semanticBody", () => {
  it("shows the query_parse_error banner and never falls through to the 0-chunks summary", () => {
    const markup = markupFor(envelope({ query_parse_error: "empty value for filter 'repo:'" }));
    expect(markup).toContain("empty value for filter");
    expect(markup).not.toContain("chunk");
  });

  it("shows the unsupported_filter banner with its reason and never falls through to results", () => {
    const markup = markupFor(
      envelope({
        unsupported_filter: "commit:",
        reason: "commit: is not a semantic filter; use search_code for commit-scoped lookups",
      })
    );
    expect(markup).toContain("commit:");
    expect(markup).toContain("use search_code");
    expect(markup).not.toContain("0 chunks");
  });

  it("shows the unsupported_filter banner for a negated query with the negation remedy", () => {
    const markup = markupFor(
      envelope({
        unsupported_filter: "-",
        reason:
          "negation is not supported in semantic queries; remove the leading '-' or quote the " +
          "term to search it as text",
      })
    );
    expect(markup).toContain("negation is not supported in semantic queries");
    expect(markup).not.toContain("chunk");
  });

  it("shows the nothing_to_embed banner with its reason and never falls through to results", () => {
    const markup = markupFor(
      envelope({
        nothing_to_embed: true,
        reason: "the query has no text left to embed after filters were removed",
      })
    );
    expect(markup).toContain("no text left to embed");
    expect(markup).not.toContain("0 chunks");
  });

  it("falls through to the results summary when no error field is set", () => {
    const markup = markupFor(envelope({ count: 0, results: [] }));
    expect(markup).toContain("0 chunks");
  });

  it("shows the disabled banner with its reason when the envelope itself reports disabled", () => {
    const markup = markupFor(envelope({ semantic_enabled: false, reason: "flag is off" }));
    expect(markup).toContain("not enabled for this deployment");
    expect(markup).toContain("flag is off");
  });
});
