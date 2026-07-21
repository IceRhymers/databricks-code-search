import { describe, expect, it } from "vitest";
import type { SemanticResult } from "../api/client";
import { chunkHref } from "./ChunkCard";

function result(overrides: Partial<SemanticResult>): SemanticResult {
  return {
    repo: "acme/widgets",
    file: "src/auth.py",
    chunk_index: 0,
    content: "short\na much longer needle line\nmid",
    start_line: null,
    end_line: null,
    rrf_score: 0.5,
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
