import { describe, expect, it } from "vitest";
import { initialSearchState, searchReducer, type SearchEnvelope } from "./searchReducer";

function envelope(overrides: Partial<SearchEnvelope> = {}): SearchEnvelope {
  return {
    query: "foo",
    file_count: 1,
    match_count: 1,
    duration_ns: 1000,
    files: [{ repo: "r1", file: "a.py", language: "python", branches: ["HEAD"], content_sha: "sha-a", permalink_branch: null, matches: [] }],
    truncated: false,
    truncation_reason: null,
    regex_incompatible: false,
    query_too_broad: false,
    query_parse_error: null,
    no_content_atom: false,
    zero_width_only_atoms: false,
    next_cursor: null,
    ...overrides,
  };
}

describe("searchReducer", () => {
  it("search_start resets accumulated state and enters loading", () => {
    const dirty = { ...initialSearchState, files: [{} as never], cursor: "stale" };
    const next = searchReducer(dirty, { type: "search_start", query: "bar" });
    expect(next.query).toBe("bar");
    expect(next.status).toBe("loading");
    expect(next.files).toEqual([]);
    expect(next.cursor).toBeNull();
    expect(next.hasSearched).toBe(true);
  });

  it("search_success sets page 1 results and cursor from next_cursor", () => {
    const loading = searchReducer(initialSearchState, { type: "search_start", query: "foo" });
    const next = searchReducer(loading, {
      type: "search_success",
      payload: envelope({ next_cursor: "abc" }),
    });
    expect(next.status).toBe("idle");
    expect(next.files).toHaveLength(1);
    expect(next.fileCount).toBe(1);
    expect(next.matchCount).toBe(1);
    expect(next.cursor).toBe("abc");
  });

  it("load_more_success appends files and advances the cursor (page 2)", () => {
    let state = searchReducer(initialSearchState, { type: "search_start", query: "foo" });
    state = searchReducer(state, {
      type: "search_success",
      payload: envelope({
        files: [{ repo: "r1", file: "a.py", language: "python", branches: ["HEAD"], content_sha: "sha-a", permalink_branch: null, matches: [] }],
        file_count: 1,
        match_count: 1,
        next_cursor: "page2",
      }),
    });
    state = searchReducer(state, { type: "load_more_start" });
    expect(state.status).toBe("loading_more");

    state = searchReducer(state, {
      type: "load_more_success",
      payload: envelope({
        files: [{ repo: "r1", file: "b.py", language: "python", branches: ["HEAD"], content_sha: "sha-b", permalink_branch: null, matches: [] }],
        file_count: 1,
        match_count: 2,
        next_cursor: null,
      }),
    });
    expect(state.status).toBe("idle");
    expect(state.files.map((f) => f.file)).toEqual(["a.py", "b.py"]);
    expect(state.fileCount).toBe(2);
    expect(state.matchCount).toBe(3);
    expect(state.cursor).toBeNull();
  });

  it("null next_cursor means exhausted: Load-more has nothing further to fetch", () => {
    const loading = searchReducer(initialSearchState, { type: "search_start", query: "foo" });
    const next = searchReducer(loading, {
      type: "search_success",
      payload: envelope({ next_cursor: null }),
    });
    expect(next.cursor).toBeNull();
  });

  it("banners reflect only the latest page, not an OR across pages", () => {
    let state = searchReducer(initialSearchState, { type: "search_start", query: "foo" });
    state = searchReducer(state, {
      type: "search_success",
      payload: envelope({ truncated: true, truncation_reason: "byte_cap", next_cursor: "p2" }),
    });
    expect(state.banners.truncated).toBe(true);

    state = searchReducer(state, { type: "load_more_start" });
    state = searchReducer(state, {
      type: "load_more_success",
      payload: envelope({ truncated: false, truncation_reason: null, next_cursor: null }),
    });
    expect(state.banners.truncated).toBe(false);
    expect(state.banners.truncationReason).toBeNull();
  });

  it("search_error surfaces the error and clears any stale cursor", () => {
    const loading = searchReducer(initialSearchState, { type: "search_start", query: "foo" });
    const next = searchReducer(loading, { type: "search_error", error: "network down" });
    expect(next.status).toBe("error");
    expect(next.error).toBe("network down");
    expect(next.cursor).toBeNull();
  });

  it("load_more_error keeps existing files but flips status to error", () => {
    let state = searchReducer(initialSearchState, { type: "search_start", query: "foo" });
    state = searchReducer(state, {
      type: "search_success",
      payload: envelope({ next_cursor: "p2" }),
    });
    const filesBefore = state.files;
    state = searchReducer(state, { type: "load_more_start" });
    state = searchReducer(state, { type: "load_more_error", error: "timeout" });
    expect(state.status).toBe("error");
    expect(state.error).toBe("timeout");
    expect(state.files).toBe(filesBefore);
  });
});
