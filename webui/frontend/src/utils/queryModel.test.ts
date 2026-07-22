import { describe, expect, it } from "vitest";
import { deriveChips, recognize, setFieldAtom, toggleBranchAtom } from "./queryModel";
import type { RepoInfo } from "../api/client";

function repo(name: string, branches: string[]): RepoInfo {
  return { name, branches, index_time: null, default_branch: branches[0] ?? null, last_indexed_commit: null };
}

const REPOS: RepoInfo[] = [repo("acme", ["main", "dev", "feature/x"]), repo("widgets", ["main"])];

describe("recognize", () => {
  it.each([
    "repo:acme lang:go foo",
    "",
    "Repo:acme",
    "foo -",
    "case:yes repo:acme foo",
    "repo:acme repo:widgets",
    "repo:acme branch:main foo",
    "repo:acme branch:main branch:dev",
    "repo:acme branch:or",
    "sym:Handler",
    "file:*.py",
    "commit:abc1234",
    "repo:acme commit:abc1234 foo",
  ])("classifies %j as safe", (query) => {
    expect(recognize(query).safe).toBe(true);
  });

  it.each([
    'repo:"a b"',
    "repo:/re/",
    "/re/",
    '"quoted content"',
    "repo:a OR repo:b",
    "repo:a or repo:b",
    "repo:a Or repo:b",
    "repo:a oR repo:b",
    "(repo:a foo)",
    "repo:acme (foo)",
    "repo:",
    'repo:acme "unterminated',
    "content:x",
    "r:foo",
    "case:maybe",
    "-foo",
    "-repo:acme",
    "-foo bar",
    "-(foo bar)",
    "--foo",
    "foo -bar",
  ])("classifies %j as unsafe", (query) => {
    expect(recognize(query).safe).toBe(false);
  });

  it("recognizes an empty query as safe with no atoms", () => {
    const model = recognize("");
    expect(model).toEqual({ safe: true, source: "", atoms: [] });
  });

  it("bails to unsafe on a token-initial '-' (negation) on any operand, not just the first", () => {
    // A leading '-' parses to a Not(...) node on the Python side; its structure a flat
    // atom-rewrite cannot represent, so the whole query is unsafe -- and this holds for a '-'
    // beginning ANY token, not only i === 0.
    expect(recognize("-repo:acme").safe).toBe(false);
    expect(recognize("foo -bar").safe).toBe(false);
    expect(recognize("-(foo bar)").safe).toBe(false);
    expect(recognize("--foo").safe).toBe(false);
  });

  it("keeps a non-negating '-' (at EOF, before whitespace or ')') safe as a literal", () => {
    // The compatibility-preserving fallthrough: a '-' whose next char is missing, whitespace,
    // or ')' is NOT negation -- it stays a literal bareword, mirroring the Python scanner.
    const trailing = recognize("foo -");
    expect(trailing.safe).toBe(true);
    if (trailing.safe) {
      expect(trailing.atoms).toEqual([
        { field: null, value: "foo", start: 0, end: 3 },
        { field: null, value: "-", start: 4, end: 5 },
      ]);
    }
    expect(recognize("a - b").safe).toBe(true);
  });

  it("treats an uppercase 'Repo:' as a content atom, not a repo filter", () => {
    const model = recognize("Repo:acme");
    expect(model.safe).toBe(true);
    if (model.safe) expect(model.atoms).toEqual([{ field: null, value: "Repo:acme", start: 0, end: 9 }]);
  });

  it("allows two repo atoms", () => {
    const model = recognize("repo:acme repo:widgets");
    expect(model.safe).toBe(true);
    if (model.safe) expect(model.atoms).toHaveLength(2);
  });

  it("allows two branch atoms", () => {
    const model = recognize("repo:acme branch:main branch:dev");
    expect(model.safe).toBe(true);
    if (model.safe) expect(model.atoms.filter((a) => a.field === "branch")).toHaveLength(2);
  });

  it("allows a bare 'or' as a branch field value (not the OR operator)", () => {
    const model = recognize("repo:acme branch:or");
    expect(model.safe).toBe(true);
    if (model.safe) expect(model.atoms).toContainEqual({ field: "branch", value: "or", start: 10, end: 19 });
  });

  it("parses a `commit:` atom as a free-text field", () => {
    const model = recognize("commit:abc1234");
    expect(model.safe).toBe(true);
    if (model.safe) expect(model.atoms).toEqual([{ field: "commit", value: "abc1234", start: 0, end: 14 }]);
  });

  it("allows a commit atom alongside other atoms", () => {
    const model = recognize("repo:acme commit:abc1234 foo");
    expect(model.safe).toBe(true);
    if (model.safe) expect(model.atoms.filter((a) => a.field === "commit")).toEqual([
      { field: "commit", value: "abc1234", start: 10, end: 24 },
    ]);
  });
});

describe("deriveChips", () => {
  it("surfaces the sole repo atom and offers that repo's branches", () => {
    const chips = deriveChips(recognize("repo:acme foo"), REPOS);
    expect(chips.editable).toBe(true);
    expect(chips.repoActive).toBe("acme");
    expect(chips.branch).toEqual({ available: true, options: ["main", "dev", "feature/x"], active: null });
  });

  it("surfaces the active branch when exactly one branch atom is present", () => {
    const chips = deriveChips(recognize("repo:acme branch:dev"), REPOS);
    expect(chips.branch.active).toBe("dev");
  });

  it("stays editable but hides the branch group for an unknown repo", () => {
    const chips = deriveChips(recognize("repo:unknown foo"), REPOS);
    expect(chips.editable).toBe(true);
    expect(chips.branch.available).toBe(false);
  });

  it("hides repoActive and the branch group when two repo atoms are present", () => {
    const chips = deriveChips(recognize("repo:acme repo:widgets"), REPOS);
    expect(chips.repoActive).toBeNull();
    expect(chips.branch.available).toBe(false);
  });

  it("hides the branch group when more than one branch atom is present", () => {
    const chips = deriveChips(recognize("repo:acme branch:a branch:b"), REPOS);
    expect(chips.branch.available).toBe(false);
  });

  it("hides the branch group with no repo atom at all", () => {
    const chips = deriveChips(recognize("foo"), REPOS);
    expect(chips.branch.available).toBe(false);
  });

  it("disables everything for an unsafe query", () => {
    const chips = deriveChips(recognize("(foo)"), REPOS);
    expect(chips).toEqual({ editable: false, repoActive: null, langActive: null, branch: { available: false, options: [], active: null } });
  });

  it("disables everything for a negated query, even one naming a known repo", () => {
    // Negation makes the whole query unsafe (see `recognize`'s token-initial '-' check), so
    // chips must not surface a stale repoActive/branch group from before the '-' was typed.
    const chips = deriveChips(recognize("-repo:acme foo"), REPOS);
    expect(chips).toEqual({ editable: false, repoActive: null, langActive: null, branch: { available: false, options: [], active: null } });
  });

  it("derives no chip state from a commit atom -- commits are free-text, never enumerable", () => {
    const withCommit = deriveChips(recognize("repo:acme commit:abc1234 foo"), REPOS);
    const withoutCommit = deriveChips(recognize("repo:acme foo"), REPOS);
    expect(withCommit).toEqual(withoutCommit);
  });
});

describe("editors", () => {
  it("appends a bare branch value", () => {
    const model = recognize("repo:acme");
    expect(model.safe && toggleBranchAtom(model, "main")).toBe("repo:acme branch:main");
  });

  it("appends a bare branch value containing a mid-value slash", () => {
    const model = recognize("repo:acme");
    expect(model.safe && toggleBranchAtom(model, "feature/x")).toBe("repo:acme branch:feature/x");
  });

  it("quotes and escapes a branch value containing a quote, and the result is unsafe on re-recognition", () => {
    const model = recognize("repo:acme");
    const next = model.safe ? toggleBranchAtom(model, 'has"quote') : "";
    expect(next).toBe('repo:acme branch:"has\\"quote"');
    expect(recognize(next).safe).toBe(false);
  });

  it("quotes a branch value containing a space", () => {
    const model = recognize("repo:acme");
    expect(model.safe && toggleBranchAtom(model, "a b")).toBe('repo:acme branch:"a b"');
  });

  it("does not quote the bare value 'or'", () => {
    const model = recognize("repo:acme");
    expect(model.safe && toggleBranchAtom(model, "or")).toBe("repo:acme branch:or");
  });

  it("removes the sole matching branch atom on toggle-off", () => {
    const model = recognize("repo:acme branch:main foo");
    expect(model.safe && toggleBranchAtom(model, "main")).toBe("repo:acme foo");
  });

  it("clears the repo field entirely when toggled off", () => {
    const model = recognize("repo:acme foo");
    expect(model.safe && setFieldAtom(model, "repo", null)).toBe("foo");
  });

  it("replaces the sole repo atom with a new one", () => {
    const model = recognize("foo repo:acme");
    expect(model.safe && setFieldAtom(model, "repo", "widgets")).toBe("foo repo:widgets");
  });

  it("appends a new lang atom when none existed", () => {
    const model = recognize("repo:acme foo");
    expect(model.safe && setFieldAtom(model, "lang", "go")).toBe("repo:acme foo lang:go");
  });

  it("appends a branch atom after existing case/repo/content atoms", () => {
    const model = recognize("case:yes repo:acme foo");
    expect(model.safe && setFieldAtom(model, "branch", "main")).toBe("case:yes repo:acme foo branch:main");
  });

  it("collapses two repo atoms down to the clicked one, keeping other atoms", () => {
    const model = recognize("repo:a repo:b foo");
    expect(model.safe && setFieldAtom(model, "repo", "a")).toBe("foo repo:a");
  });

  it("collapses two repo atoms down to a third value", () => {
    const model = recognize("repo:a repo:b");
    expect(model.safe && setFieldAtom(model, "repo", "c")).toBe("repo:c");
  });

  it("toggles off the sole active repo atom", () => {
    const model = recognize("repo:a foo");
    expect(model.safe && setFieldAtom(model, "repo", null)).toBe("foo");
  });

  it("round-trips: a bare-safe edit output re-recognizes as safe", () => {
    const model = recognize("repo:acme");
    const next = model.safe ? toggleBranchAtom(model, "feature/x") : "";
    expect(recognize(next).safe).toBe(true);
  });

  it("appends a commit atom via setFieldAtom and round-trips through re-recognition", () => {
    const model = recognize("repo:acme foo");
    const next = model.safe ? setFieldAtom(model, "commit", "abc1234") : "";
    expect(next).toBe("repo:acme foo commit:abc1234");
    const reParsed = recognize(next);
    expect(reParsed.safe).toBe(true);
    if (reParsed.safe) {
      expect(reParsed.atoms).toContainEqual({ field: "commit", value: "abc1234", start: 14, end: 28 });
    }
  });

  it("clears a commit atom entirely when toggled off via setFieldAtom(null)", () => {
    const model = recognize("commit:abc1234 foo");
    expect(model.safe && setFieldAtom(model, "commit", null)).toBe("foo");
  });
});
