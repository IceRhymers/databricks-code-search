// vitest runs with environment: "node" (vite.config.ts) -- no jsdom, no @testing-library.
// renderToStaticMarkup gives us plain HTML strings to assert substrings against without either.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ResultsList } from "./ResultsList";
import type { SearchFile } from "../utils/searchReducer";

function file(overrides: Partial<SearchFile> = {}): SearchFile {
  return {
    repo: "acme/widgets",
    file: "a.py",
    language: "python",
    branches: ["main"],
    content_sha: "sha-1",
    permalink_branch: "main",
    matches: [],
    ...overrides,
  };
}

describe("ResultsList", () => {
  it("threads distinct permalink_branch values into distinct file-header hrefs", () => {
    const markup = renderToStaticMarkup(
      <ResultsList
        files={[
          file({ content_sha: "sha-main", permalink_branch: "main" }),
          file({ content_sha: "sha-feature", permalink_branch: "feature" }),
        ]}
      />
    );
    // renderToStaticMarkup escapes `&` to `&amp;` in the query string, so assert the substring
    // without a leading `&` (e.g. "branch=main", not "&branch=main").
    expect(markup).toContain("branch=main");
    expect(markup).toContain("branch=feature");
  });

  it("omits branch entirely from the href when permalink_branch is null", () => {
    const markup = renderToStaticMarkup(<ResultsList files={[file({ permalink_branch: null })]} />);
    expect(markup).not.toContain("branch=");
  });
});
