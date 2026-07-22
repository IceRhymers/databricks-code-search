import { useEffect, useRef, useState } from "react";
import { ApiError, getSemanticStatus, semanticSearch, type SemanticEnvelope } from "../api/client";
import { ChunkCard } from "../components/ChunkCard";
import { replaceRoute } from "../router";

type Status = "idle" | "loading" | "error";

// Pure render-decision function, extracted from the component so the banner/results branching
// is testable via renderToStaticMarkup without jsdom or hook-driven state (this repo's vitest
// runs in a plain "node" environment). Render order mirrors the plan: loading, request error,
// disabled state, not-migrated banner, then the three filter-grammar error states, then
// results -- all mutually exclusive by construction (status/envelope shape), so returning early
// from each branch keeps an error envelope from ever falling through to "0 chunks".
export function semanticBody(
  status: Status,
  error: string | null,
  envelope: SemanticEnvelope | null,
  enabled: boolean | null
): JSX.Element | null {
  if (status === "loading") return <div className="result-summary">Searching…</div>;
  if (status === "error") return <div className="banner error">{error}</div>;
  if (!envelope) {
    if (enabled === false) {
      return <div className="banner warn">Semantic search is not enabled for this deployment.</div>;
    }
    return null;
  }
  if (envelope.semantic_enabled === false) {
    return (
      <div className="banner warn">
        Semantic search is not enabled for this deployment.
        {envelope.reason ? ` ${envelope.reason}` : ""}
      </div>
    );
  }
  if (envelope.semantic_schema_missing) {
    return <div className="banner warn">{envelope.reason}</div>;
  }
  // Filter-grammar atoms (repo:/file:/lang:/branch:) are parsed in-query; these three error
  // states are mutually exclusive with each other and with a results payload, so they must
  // be checked (and returned) before falling through to the "0 chunks" results branch below.
  if (envelope.query_parse_error) {
    return <div className="banner error">{envelope.query_parse_error}</div>;
  }
  if (envelope.unsupported_filter) {
    return (
      <div className="banner error">
        {envelope.unsupported_filter}
        {envelope.reason ? ` ${envelope.reason}` : ""}
      </div>
    );
  }
  if (envelope.nothing_to_embed) {
    return (
      <div className="banner warn">
        {envelope.reason ?? "Nothing to search -- the query has no text left to embed."}
      </div>
    );
  }
  return (
    <>
      <div className="result-summary">
        {envelope.count} chunk{envelope.count === 1 ? "" : "s"}, ranked by hybrid relevance
      </div>
      {envelope.results.map((result, i) => (
        <ChunkCard key={i} result={result} />
      ))}
    </>
  );
}

export function SemanticPage({ initialQuery }: { initialQuery: string }): JSX.Element {
  const [input, setInput] = useState(initialQuery);
  const [status, setStatus] = useState<Status>("idle");
  const [envelope, setEnvelope] = useState<SemanticEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  // null = probe pending/unknown; a failed probe must stay null, not false, so it
  // doesn't show the disabled banner ahead of an actual disabled envelope.
  const [enabled, setEnabled] = useState<boolean | null>(null);
  // Guards the mount-time auto-search so StrictMode's double-invoke (dev only) can't double-fire.
  const ranInitial = useRef(false);
  const ranStatus = useRef(false);

  async function runSearch(query: string) {
    if (!query.trim()) return;
    replaceRoute(`/semantic?q=${encodeURIComponent(query)}`);
    setStatus("loading");
    try {
      const payload = await semanticSearch(query);
      setEnvelope(payload);
      setStatus("idle");
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Semantic search request failed.";
      setError(message);
      setStatus("error");
    }
  }

  useEffect(() => {
    if (ranInitial.current) return;
    ranInitial.current = true;
    if (initialQuery.trim()) {
      void runSearch(initialQuery);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (ranStatus.current) return;
    ranStatus.current = true;
    getSemanticStatus()
      .then((status) => setEnabled(status.semantic_enabled))
      .catch(() => {
        // A failed probe must not show the disabled banner -- leave enabled unknown.
      });
  }, []);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    void runSearch(input);
  }

  return (
    <div>
      <form className="search-box" onSubmit={handleSubmit}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder='e.g. "how are branch filters compiled to SQL" repo:acme/widgets'
          aria-label="Semantic search query"
          autoFocus
        />
        <button type="submit">Search</button>
      </form>
      <p className="result-summary">
        Scope with in-query <code>repo:</code>, <code>file:</code>, or <code>lang:</code> atoms
        -- the rest of the query is embedded for similarity ranking.
      </p>

      {semanticBody(status, error, envelope, enabled)}
    </div>
  );
}
