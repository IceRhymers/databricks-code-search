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

  it("renders the commit badge truncated to 12 chars when present", () => {
    const markup = renderToStaticMarkup(
      <ResultsList files={[file({ commit: "abc1234def567890fedcba9876543210" })]} />
    );
    expect(markup).toContain("commit-badge");
    expect(markup).toContain("abc1234def56");
    expect(markup).not.toContain("abc1234def567890fedcba9876543210");
  });

  it("omits the commit badge entirely when commit is absent (unscoped search)", () => {
    const markup = renderToStaticMarkup(<ResultsList files={[file()]} />);
    expect(markup).not.toContain("commit-badge");
  });

  it("renders a /references?symbol= link next to a symbol match", () => {
    const markup = renderToStaticMarkup(
      <ResultsList
        files={[
          file({
            matches: [
              {
                line: 2,
                text: "func Handler() {}",
                byte_ranges: [],
                symbols: [{ name: "Handler", kind: "function" }],
              },
            ],
          }),
        ]}
      />
    );
    expect(markup).toContain('href="/references?symbol=Handler"');
    expect(markup).toContain(">refs<");
  });
});
