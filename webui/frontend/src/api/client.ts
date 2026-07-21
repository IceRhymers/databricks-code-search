// Thin fetch wrappers around the webui FastAPI backend (webui/main.py, WS-B). Kept separate
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
  rrf_score: number;
}

export interface SemanticEnvelope {
  query: string;
  semantic_enabled: boolean;
  backend?: string;
  results: SemanticResult[];
  count: number;
  reason?: string;
  semantic_schema_missing?: boolean;
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
