// vitest runs with environment: "node" (vite.config.ts, no jsdom) -- router.ts reads
// `window.location`/`window.history`/`window.addEventListener` both at module scope and inside
// parseLocation, so there is no bare `window` global here to read. We stub a minimal fake
// window via vi.stubGlobal and re-import the module fresh (vi.resetModules) for each case,
// rather than adding a jsdom/@testing-library dependency.
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Route } from "./router";

function fakeWindow(href: string) {
  const url = new URL(href, "http://localhost");
  return {
    location: { pathname: url.pathname, search: url.search, hash: url.hash, href: url.href },
    history: { replaceState: vi.fn(), pushState: vi.fn() },
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
}

async function parseLocationFor(href: string): Promise<Route> {
  vi.stubGlobal("window", fakeWindow(href));
  vi.resetModules();
  const { parseLocation } = await import("./router");
  return parseLocation();
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("parseLocation", () => {
  it("parses /file?repo=..&path=..&branch=x with branch \"x\"", async () => {
    const route = await parseLocationFor("/file?repo=acme%2Fwidgets&path=a.py&branch=x");
    expect(route).toEqual({
      page: "file",
      repo: "acme/widgets",
      path: "a.py",
      line: null,
      find: null,
      branch: "x",
    });
  });

  it("parses /file?repo=..&path=.. with no branch param as branch: null", async () => {
    const route = await parseLocationFor("/file?repo=acme%2Fwidgets&path=a.py");
    expect(route).toMatchObject({ page: "file", branch: null });
  });
});
