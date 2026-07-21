import { useEffect, useRef, useState } from "react";
import { ApiError, semanticSearch, type SemanticEnvelope } from "../api/client";
import { ChunkCard } from "../components/ChunkCard";
import { replaceRoute } from "../router";

type Status = "idle" | "loading" | "error";

export function SemanticPage({ initialQuery }: { initialQuery: string }): JSX.Element {
  const [input, setInput] = useState(initialQuery);
  const [status, setStatus] = useState<Status>("idle");
  const [envelope, setEnvelope] = useState<SemanticEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Guards the mount-time auto-search so StrictMode's double-invoke (dev only) can't double-fire.
  const ranInitial = useRef(false);

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

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    void runSearch(input);
  }

  // Render order mirrors the plan: disabled state, then not-migrated banner, then error,
  // then results -- but these are mutually exclusive by construction (status/envelope shape),
  // so a single derived body keeps the branches from overlapping.
  function renderBody(): JSX.Element | null {
    if (status === "loading") return <div className="result-summary">Searching…</div>;
    if (status === "error") return <div className="banner error">{error}</div>;
    if (!envelope) return null;
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

  return (
    <div>
      <form className="search-box" onSubmit={handleSubmit}>
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder='e.g. "how are branch filters compiled to SQL"'
          aria-label="Semantic search query"
          autoFocus
        />
        <button type="submit">Search</button>
      </form>

      {renderBody()}
    </div>
  );
}
