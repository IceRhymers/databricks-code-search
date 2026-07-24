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
      endLine: null,
      find: null,
      branch: "x",
    });
  });

  it("parses /file?repo=..&path=.. with no branch param as branch: null", async () => {
    const route = await parseLocationFor("/file?repo=acme%2Fwidgets&path=a.py");
    expect(route).toMatchObject({ page: "file", branch: null });
  });

  it("parses a #L10-L24 range anchor into line/endLine", async () => {
    const route = await parseLocationFor("/file?repo=acme%2Fwidgets&path=a.py#L10-L24");
    expect(route).toMatchObject({ page: "file", line: 10, endLine: 24 });
  });

  it("parses a single #L7 anchor with endLine: null", async () => {
    const route = await parseLocationFor("/file?repo=acme%2Fwidgets&path=a.py#L7");
    expect(route).toMatchObject({ page: "file", line: 7, endLine: null });
  });

  it("parses /references?symbol=X&branch=Y", async () => {
    const route = await parseLocationFor("/references?symbol=process&branch=feature%2Fx");
    expect(route).toEqual({
      page: "graph",
      mode: "references",
      symbol: "process",
      branch: "feature/x",
    });
  });

  it("parses /references with no branch as branch: null", async () => {
    const route = await parseLocationFor("/references?symbol=process");
    expect(route).toMatchObject({ page: "graph", mode: "references", branch: null });
  });

  it("parses /imports?repo=R&direction=imports", async () => {
    const route = await parseLocationFor("/imports?repo=acme%2Fwidgets&direction=imports");
    expect(route).toEqual({
      page: "graph",
      mode: "imports",
      repo: "acme/widgets",
      target: "",
      direction: "imports",
      branch: null,
    });
  });

  it("parses /imports?target=T&direction=imported_by", async () => {
    const route = await parseLocationFor("/imports?target=os.path&direction=imported_by");
    expect(route).toEqual({
      page: "graph",
      mode: "imports",
      repo: "",
      target: "os.path",
      direction: "imported_by",
      branch: null,
    });
  });

  it("parses /imports?direction=bogus verbatim, no validation/coercion", async () => {
    const route = await parseLocationFor("/imports?direction=bogus");
    expect(route).toMatchObject({ page: "graph", mode: "imports", direction: "bogus" });
  });

  it("defaults /imports direction to \"imports\" when omitted", async () => {
    const route = await parseLocationFor("/imports?repo=acme%2Fwidgets");
    expect(route).toMatchObject({ page: "graph", mode: "imports", direction: "imports" });
  });
});
