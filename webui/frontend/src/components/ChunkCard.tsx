import type { SemanticResult } from "../api/client";
import { extractNeedle } from "../utils/chunkAnchor";

export function chunkHref(result: SemanticResult): string {
  const params = new URLSearchParams({ repo: result.repo, path: result.file });
  // Exact anchor when the chunk carries its line range (issue #44); rows indexed before
  // line tracking have nulls and keep the needle-match fallback below.
  if (result.start_line !== null && result.end_line !== null) {
    const range =
      result.end_line > result.start_line
        ? `#L${result.start_line}-L${result.end_line}`
        : `#L${result.start_line}`;
    return `/file?${params.toString()}${range}`;
  }
  const needle = extractNeedle(result.content);
  if (needle) params.set("find", needle);
  return `/file?${params.toString()}`;
}

/** One ranked semantic chunk. Card order and count come from props verbatim -- never re-sorted. */
export function ChunkCard({ result }: { result: SemanticResult }): JSX.Element {
  return (
    <div className="chunk-card">
      <div className="chunk-card-header">
        <a href={chunkHref(result)}>
          {result.repo}/{result.file}
        </a>
        <span className="lang">chunk {result.chunk_index}</span>
        <span className="lang">score {result.rrf_score.toFixed(4)}</span>
      </div>
      <pre className="chunk-card-body">{result.content}</pre>
    </div>
  );
}
