// State machine for the search page's paginated results. The wire contract (ralplan-35 WS-A/C):
// the API always passes `cursor=` explicitly, so every envelope carries `next_cursor`
// (string | null); a non-null cursor means more candidate rows remain and drives the
// "Load more" button. Banners (`truncated`/`truncation_reason`, `query_too_broad`,
// `regex_incompatible`) reflect ONLY the most recently fetched page, never an OR across pages
// -- a clean continuation page must be able to clear a banner a prior page raised.

export interface SymbolTag {
  name: string;
  kind: string;
}

export interface SearchMatch {
  line: number | null;
  text: string;
  byte_ranges: [number, number][];
  symbols?: SymbolTag[];
}

export interface SearchFile {
  repo: string;
  file: string;
  language: string | null;
  branches: string[];
  content_sha: string;
  permalink_branch: string | null;
  matches: SearchMatch[];
}

export interface SearchEnvelope {
  query: string;
  file_count: number;
  match_count: number;
  duration_ns: number;
  files: SearchFile[];
  truncated: boolean;
  truncation_reason: string | null;
  regex_incompatible: boolean;
  query_too_broad: boolean;
  query_parse_error: string | null;
  no_content_atom: boolean;
  zero_width_only_atoms: boolean;
  next_cursor?: string | null;
}

export interface SearchBanners {
  truncated: boolean;
  truncationReason: string | null;
  queryTooBroad: boolean;
  regexIncompatible: boolean;
  queryParseError: string | null;
}

export type SearchStatus = "idle" | "loading" | "loading_more" | "error";

export interface SearchState {
  query: string;
  status: SearchStatus;
  files: SearchFile[];
  fileCount: number;
  matchCount: number;
  cursor: string | null;
  hasSearched: boolean;
  banners: SearchBanners;
  error: string | null;
}

export const initialSearchState: SearchState = {
  query: "",
  status: "idle",
  files: [],
  fileCount: 0,
  matchCount: 0,
  cursor: null,
  hasSearched: false,
  banners: {
    truncated: false,
    truncationReason: null,
    queryTooBroad: false,
    regexIncompatible: false,
    queryParseError: null,
  },
  error: null,
};

export type SearchAction =
  | { type: "search_start"; query: string }
  | { type: "search_success"; payload: SearchEnvelope }
  | { type: "search_error"; error: string }
  | { type: "load_more_start" }
  | { type: "load_more_success"; payload: SearchEnvelope }
  | { type: "load_more_error"; error: string };

function bannersFrom(payload: SearchEnvelope): SearchBanners {
  return {
    truncated: payload.truncated,
    truncationReason: payload.truncation_reason,
    queryTooBroad: payload.query_too_broad,
    regexIncompatible: payload.regex_incompatible,
    queryParseError: payload.query_parse_error,
  };
}

export function searchReducer(state: SearchState, action: SearchAction): SearchState {
  switch (action.type) {
    case "search_start":
      return {
        ...initialSearchState,
        query: action.query,
        status: "loading",
        hasSearched: true,
      };
    case "search_success":
      return {
        ...state,
        status: "idle",
        files: action.payload.files,
        fileCount: action.payload.file_count,
        matchCount: action.payload.match_count,
        cursor: action.payload.next_cursor ?? null,
        banners: bannersFrom(action.payload),
        error: null,
      };
    case "search_error":
      return { ...state, status: "error", error: action.error, cursor: null };
    case "load_more_start":
      return { ...state, status: "loading_more" };
    case "load_more_success":
      return {
        ...state,
        status: "idle",
        files: [...state.files, ...action.payload.files],
        fileCount: state.fileCount + action.payload.file_count,
        matchCount: state.matchCount + action.payload.match_count,
        cursor: action.payload.next_cursor ?? null,
        banners: bannersFrom(action.payload),
        error: null,
      };
    case "load_more_error":
      return { ...state, status: "error", error: action.error };
    default:
      return state;
  }
}
