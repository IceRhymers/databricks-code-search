// Thin fetch wrappers around the webui FastAPI backend (webui/main.py). Kept separate
// from the reducer/types module (../utils/searchReducer) so the wire types have exactly one
// home; this file only knows how to reach the routes.

import type { SearchEnvelope } from "../utils/searchReducer";

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    // Every webui/main.py error route returns 400/404 JSON shaped {error: string} (parse
    // errors, bad cursors, invalid parameters, missing files) -- no `position` field: the
    // backend can't recover a QueryParseError's position once app.service has folded it into
    // the payload's query_parse_error string (see webui/main.py:api_search's docstring).
    const body = await res.json().catch(() => null);
    const message = (body && typeof body.error === "string" ? body.error : null) ?? res.statusText;
    throw new ApiError(message, res.status);
  }
  return (await res.json()) as T;
}

export function searchCode(query: string, opts: { limit?: number; cursor?: string | null } = {}): Promise<SearchEnvelope> {
  const params = new URLSearchParams({ q: query });
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.cursor) params.set("cursor", opts.cursor);
  return getJson<SearchEnvelope>(`/api/search?${params.toString()}`);
}

export interface SemanticResult {
  repo: string;
  file: string;
  chunk_index: number;
  content: string;
  // 1-based inclusive line range; null for chunks indexed before line tracking.
  start_line: number | null;
  end_line: number | null;
  rrf_score: number;
  // 1 - cosine_distance, recomputed in the outer select; null for chunks indexed before
  // embedding (pre-embedding rows still rank via the BM25 leg alone).
  similarity: number | null;
}

export interface SemanticEnvelope {
  query: string;
  semantic_enabled: boolean;
  results: SemanticResult[];
  count: number;
  reason?: string;
  semantic_schema_missing?: boolean;
  // Mutually exclusive with each other and with a normal results payload: filter-grammar
  // atoms (repo:/file:/lang:/branch:) are parsed in-query, so a malformed atom, an atom this
  // surface doesn't support, or a query that reduces to filters-only/empty all short-circuit
  // to results: [], count: 0 plus one of these fields (see webui/main.py::api_semantic).
  query_parse_error?: string;
  unsupported_filter?: string;
  nothing_to_embed?: boolean;
}

export function semanticSearch(query: string, opts: { limit?: number; branch?: string } = {}): Promise<SemanticEnvelope> {
  const params = new URLSearchParams({ q: query });
  if (opts.limit) params.set("limit", String(opts.limit));
  if (opts.branch) params.set("branch", opts.branch);
  return getJson<SemanticEnvelope>(`/api/semantic?${params.toString()}`);
}

export function getSemanticStatus(): Promise<{ semantic_enabled: boolean }> {
  return getJson<{ semantic_enabled: boolean }>("/api/semantic/status");
}

export interface FileResponse {
  repo: string;
  path: string;
  branch: string;
  content: string | null;
  found: boolean;
  // Indexed commit for `branch`, sourced from repo_branches; present only when the branch
  // resolves (webui/main.py's get_file_payload), absent otherwise.
  commit?: string | null;
}

export function getFile(repo: string, path: string, branch?: string | null): Promise<FileResponse> {
  const params = new URLSearchParams({ repo, path });
  if (branch) params.set("branch", branch);
  return getJson<FileResponse>(`/api/file?${params.toString()}`);
}

export interface RepoInfo {
  name: string;
  branches: string[];
  index_time: string | null;
  default_branch: string | null;
  last_indexed_commit: string | null;
}

export interface ReposResponse {
  repos: RepoInfo[];
  count: number;
}

export function listRepos(): Promise<ReposResponse> {
  return getJson<ReposResponse>("/api/repos");
}

// -------------------------------------------------------------- reference/import graph tools
//
// Wire types for /api/references + /api/imports -- thin passthroughs over the SAME
// app/service.py builders the MCP find_references/list_imports tools wrap (see
// webui/main.py::api_references / api_imports and docs/runbooks/webui.md). CANDIDATE-SET
// semantics throughout: a site is a place that names something; its `candidates` are the
// definitions that name could plausibly mean, ranked, never collapsed to one answer.

export interface GraphCandidate {
  repo: string;
  file: string;
  line: number;
  name: string;
  kind: string;
  same_repo: boolean;
  same_file: boolean;
  kind_match: boolean;
}

export interface ReferenceSite {
  repo: string;
  file: string;
  line: number;
  edge_kind: string;
  target_name: string;
  enclosing_symbol: { name: string; kind: string } | null;
  resolution: "unique" | "ambiguous" | "unresolved";
  // True pre-cap count -- correct even when `candidates` itself is capped.
  candidate_count: number;
  candidates_truncated: boolean;
  candidates: GraphCandidate[];
}

interface ResolutionSummary {
  unique: number;
  ambiguous: number;
  unresolved: number;
}

// Fields shared by both envelopes (app.service._reference_result_to_payload).
interface ReferenceEnvelopeBase {
  sites: ReferenceSite[];
  site_count: number;
  resolution_summary: ResolutionSummary;
  truncated: boolean;
  truncation_reason: string | null;
}

export interface ReferencesEnvelope extends ReferenceEnvelopeBase {
  query: string;
  kind: "references";
  symbol: string;
  branch: string | null;
  // Folded QueryTooBroadError -- never an exception; a structured signal like every other
  // recoverable condition on this surface.
  query_too_broad: boolean;
}

export interface ImportsEnvelope extends ReferenceEnvelopeBase {
  query: string;
  kind: "imports";
  direction: string;
  repo: string | null;
  // False is a structured "no such repo" miss; always true when no repo scope was requested.
  repo_known: boolean;
  target: string | null;
  branch: string | null;
  query_too_broad: boolean;
  // PRE-DB validation states -- mutually exclusive with each other and with a results payload,
  // each with a remedy `reason` (see app.service.list_imports_payload).
  unsupported_direction?: string;
  missing_repo?: boolean;
  missing_target?: boolean;
  reason?: string;
}

export function findReferences(
  symbol: string,
  opts: { branch?: string | null } = {}
): Promise<ReferencesEnvelope> {
  const params = new URLSearchParams({ symbol });
  if (opts.branch) params.set("branch", opts.branch);
  return getJson<ReferencesEnvelope>(`/api/references?${params.toString()}`);
}

export function listImports(opts: {
  repo?: string | null;
  target?: string | null;
  direction?: string;
  branch?: string | null;
}): Promise<ImportsEnvelope> {
  const params = new URLSearchParams();
  if (opts.repo) params.set("repo", opts.repo);
  if (opts.target) params.set("target", opts.target);
  if (opts.direction) params.set("direction", opts.direction);
  if (opts.branch) params.set("branch", opts.branch);
  return getJson<ImportsEnvelope>(`/api/imports?${params.toString()}`);
}
