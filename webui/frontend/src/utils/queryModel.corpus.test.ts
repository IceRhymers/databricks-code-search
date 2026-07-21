// Cross-language parity gate: this corpus (queryModel.corpus.json) is shared with
// tests/unit/test_query_corpus_parity.py, which asserts the same entries against the real
// Python parser (app/query/parser.py). Keeping one JSON file as the source of truth for
// both sides is what makes "safe" here mean "app/query/parser.py agrees this is a flat
// AND of these exact atoms", not just "our TS port thinks so".
import { describe, expect, it } from "vitest";
import { recognize } from "./queryModel";
import corpus from "./queryModel.corpus.json";

interface CorpusEntry {
  query: string;
  verdict: "safe" | "unsafe";
  python_parses: boolean;
  atoms?: { field: string | null; value: string; start: number; end: number }[];
}

describe("queryModel corpus parity", () => {
  it.each(corpus as CorpusEntry[])("$query", (entry) => {
    const model = recognize(entry.query);
    expect(model.safe).toBe(entry.verdict === "safe");
    if (model.safe && entry.atoms) {
      expect(model.atoms).toEqual(entry.atoms);
    }
  });
});
