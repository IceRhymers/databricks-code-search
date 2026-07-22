// vitest runs with environment: "node" (vite.config.ts) -- no jsdom, no @testing-library.
// renderToStaticMarkup gives us plain HTML strings to assert substrings against without either.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { SemanticResult } from "../api/client";
import { ChunkCard, chunkHref } from "./ChunkCard";

function result(overrides: Partial<SemanticResult>): SemanticResult {
  return {
    repo: "acme/widgets",
    file: "src/auth.py",
    chunk_index: 0,
    content: "short\na much longer needle line\nmid",
    start_line: null,
    end_line: null,
    rrf_score: 0.5,
    similarity: 0.5,
    ...overrides,
  };
}

describe("chunkHref", () => {
  it("links to an exact #L<start>-L<end> range when the chunk carries lines (issue #44)", () => {
    const href = chunkHref(result({ start_line: 10, end_line: 24 }));
    expect(href).toBe("/file?repo=acme%2Fwidgets&path=src%2Fauth.py#L10-L24");
  });

  it("collapses a single-line chunk to a plain #L<n> anchor", () => {
    const href = chunkHref(result({ start_line: 7, end_line: 7 }));
    expect(href).toBe("/file?repo=acme%2Fwidgets&path=src%2Fauth.py#L7");
  });

  it("falls back to the needle-match find= param when lines are null (pre-#44 rows)", () => {
    const href = chunkHref(result({}));
    expect(href).toContain("find=");
    expect(href).toContain(encodeURIComponent("a much longer needle line").replace(/%20/g, "+"));
    expect(href).not.toContain("#L");
  });
});

describe("ChunkCard", () => {
  it("renders similarity to three decimal places beside the rrf score", () => {
    const markup = renderToStaticMarkup(<ChunkCard result={result({ similarity: 0.81234 })} />);
    expect(markup).toContain("sim 0.812");
  });

  it("renders an em dash for a null similarity (pre-embedding rows)", () => {
    const markup = renderToStaticMarkup(<ChunkCard result={result({ similarity: null })} />);
    expect(markup).toContain("sim —");
    expect(markup).not.toContain("sim null");
  });
});
