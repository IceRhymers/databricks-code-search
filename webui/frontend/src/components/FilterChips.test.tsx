// vitest runs with environment: "node" (vite.config.ts) -- no jsdom, no @testing-library.
// renderToStaticMarkup gives us plain HTML strings to assert substrings against without either.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RepoInfo } from "../api/client";
import { deriveChips, recognize } from "../utils/queryModel";
import { FilterChips } from "./FilterChips";

function repo(name: string, branches: string[]): RepoInfo {
  return { name, branches, index_time: null, default_branch: branches[0] ?? null, last_indexed_commit: null };
}

const REPOS: RepoInfo[] = [repo("acme", ["main", "dev"])];
const LANGUAGES = ["go"];

function markupFor(query: string): string {
  const chips = deriveChips(recognize(query), REPOS);
  return renderToStaticMarkup(
    <FilterChips
      repos={REPOS}
      languages={LANGUAGES}
      chips={chips}
      onToggleRepo={() => {}}
      onToggleLanguage={() => {}}
      onToggleBranch={() => {}}
    />
  );
}

describe("FilterChips", () => {
  it("surfaces the negation sentinel in the disabled tooltip for a negated query", () => {
    // The tooltip text is the primary anchor here (not just the disabled attribute) so this
    // test fails loudly if the negation wording ever regresses out of UNSAFE_TITLE.
    const markup = markupFor("-repo:acme foo");
    expect(markup).toContain("negation (-)");
    expect(markup).toContain("disabled=\"\"");
  });

  it("hides the branch group entirely for a negated query naming a known repo", () => {
    const markup = markupFor("-repo:acme foo");
    expect(markup).not.toContain("branch:main");
    expect(markup).not.toContain("branch:dev");
  });

  it("omits the negation sentinel for a safe query", () => {
    const markup = markupFor("repo:acme foo");
    expect(markup).not.toContain("negation (-)");
  });
});
