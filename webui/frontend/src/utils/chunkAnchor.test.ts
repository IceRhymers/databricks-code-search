import { describe, expect, it } from "vitest";
import { extractNeedle, locateNeedleLine } from "./chunkAnchor";

describe("extractNeedle", () => {
  it("picks the longest non-empty trimmed line, not the first", () => {
    const chunk = "short\na much longer line here\nmid";
    expect(extractNeedle(chunk)).toBe("a much longer line here");
  });

  it("returns null for a whitespace-only chunk", () => {
    expect(extractNeedle("   \n\t\n   ")).toBeNull();
  });

  it("returns the trimmed line for a single-line chunk", () => {
    expect(extractNeedle("  the only line  ")).toBe("the only line");
  });
});

describe("locateNeedleLine", () => {
  it("returns null when the needle is absent", () => {
    expect(locateNeedleLine("hello world", "missing")).toBeNull();
  });

  it("returns line 1 and occurrences 1 for a needle at offset 0", () => {
    const content = "needle here\nsecond line\nthird line";
    expect(locateNeedleLine(content, "needle here")).toEqual({ line: 1, occurrences: 1 });
  });

  it("returns the correct 1-based line for a needle mid-file", () => {
    const content = "line one\nline two\nthe needle line\nline four";
    expect(locateNeedleLine(content, "the needle line")).toEqual({ line: 3, occurrences: 1 });
  });

  it("returns the first-occurrence line and total count when the needle appears 3 times", () => {
    const content = "alpha\nrepeat me\nbeta\nrepeat me\ngamma\nrepeat me";
    expect(locateNeedleLine(content, "repeat me")).toEqual({ line: 2, occurrences: 3 });
  });
});
