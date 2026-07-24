import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  findReferences,
  listImports,
  type ImportsEnvelope,
  type ReferencesEnvelope,
} from "../api/client";
import { SiteList } from "../components/SiteList";
import { replaceRoute, type Route } from "../router";

type Status = "idle" | "loading" | "error";
type GraphRoute = Extract<Route, { page: "graph" }>;

// Pure render-decision functions, extracted for the same reason as SemanticPage's
// semanticBody: testable via renderToStaticMarkup without hook-driven state. Strict
// early-return ordering so an error/validation envelope can never fall through to results.

export function referencesBody(
  status: Status,
  error: string | null,
  envelope: ReferencesEnvelope | null
): JSX.Element | null {
  if (status === "loading") return <div className="result-summary">Searching…</div>;
  if (status === "error") return <div className="banner error">{error}</div>;
  if (!envelope) return null;
  if (envelope.query_too_broad) {
    return (
      <div className="banner warn">
        Query too broad — results were cut short by the time budget.
      </div>
    );
  }
  return (
    <SiteList
      sites={envelope.sites}
      siteCount={envelope.site_count}
      resolutionSummary={envelope.resolution_summary}
      truncated={envelope.truncated}
      truncationReason={envelope.truncation_reason}
      branch={envelope.branch}
      emptyMessage="No reference sites."
    />
  );
}

export function importsBody(
  status: Status,
  error: string | null,
  envelope: ImportsEnvelope | null
): JSX.Element | null {
  if (status === "loading") return <div className="result-summary">Searching…</div>;
  if (status === "error") return <div className="banner error">{error}</div>;
  if (!envelope) return null;
  if (envelope.unsupported_direction !== undefined) {
    return (
      <div className="banner error">
        Unsupported direction &quot;{envelope.unsupported_direction}&quot;.
        {envelope.reason ? ` ${envelope.reason}` : ""}
      </div>
    );
  }
  if (envelope.missing_repo) {
    return (
      <div className="banner error">
        A repo is required.{envelope.reason ? ` ${envelope.reason}` : ""}
      </div>
    );
  }
  if (envelope.missing_target) {
    return (
      <div className="banner error">
        A target is required.{envelope.reason ? ` ${envelope.reason}` : ""}
      </div>
    );
  }
  if (envelope.repo_known === false) {
    return <div className="banner warn">No such repo: {envelope.repo}.</div>;
  }
  if (envelope.query_too_broad) {
    return (
      <div className="banner warn">
        Query too broad — results were cut short by the time budget.
      </div>
    );
  }
  return (
    <>
      <p className="result-summary">
        Import edges target the full dotted path as written, so most sites are external/stdlib
        and resolve &quot;unresolved&quot; — that is expected, not an error.
      </p>
      <SiteList
        sites={envelope.sites}
        siteCount={envelope.site_count}
        resolutionSummary={envelope.resolution_summary}
        truncated={envelope.truncated}
        truncationReason={envelope.truncation_reason}
        branch={envelope.branch}
        emptyMessage="No import sites."
      />
    </>
  );
}

export function GraphPage({ route }: { route: GraphRoute }): JSX.Element {
  const mode = route.mode;
  const [symbol, setSymbol] = useState(route.mode === "references" ? route.symbol : "");
  const [repo, setRepo] = useState(route.mode === "imports" ? route.repo : "");
  const [target, setTarget] = useState(route.mode === "imports" ? route.target : "");
  const [direction, setDirection] = useState(route.mode === "imports" ? route.direction : "imports");
  const [branch, setBranch] = useState(route.branch ?? "");
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [referencesEnvelope, setReferencesEnvelope] = useState<ReferencesEnvelope | null>(null);
  const [importsEnvelope, setImportsEnvelope] = useState<ImportsEnvelope | null>(null);
  // Guards the mount-time auto-run so StrictMode's double-invoke (dev only) can't double-fire.
  // App.tsx mounts GraphPage with key={route.mode}, so a references<->imports switch remounts
  // this component and re-arms the guard rather than bleeding state across modes.
  const ranInitial = useRef(false);

  async function runReferences(sym: string, br: string) {
    if (!sym.trim()) return;
    const params = new URLSearchParams({ symbol: sym });
    if (br) params.set("branch", br);
    replaceRoute(`/references?${params.toString()}`);
    setStatus("loading");
    try {
      const payload = await findReferences(sym, { branch: br || null });
      setReferencesEnvelope(payload);
      setStatus("idle");
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "References request failed.";
      setError(message);
      setStatus("error");
    }
  }

  async function runImports(r: string, t: string, dir: string, br: string) {
    const params = new URLSearchParams();
    if (r) params.set("repo", r);
    if (t) params.set("target", t);
    if (dir) params.set("direction", dir);
    if (br) params.set("branch", br);
    replaceRoute(`/imports?${params.toString()}`);
    setStatus("loading");
    try {
      const payload = await listImports({
        repo: r || null,
        target: t || null,
        direction: dir,
        branch: br || null,
      });
      setImportsEnvelope(payload);
      setStatus("idle");
    } catch (err) {
      const message = err instanceof ApiError ? err.message : "Imports request failed.";
      setError(message);
      setStatus("error");
    }
  }

  useEffect(() => {
    if (ranInitial.current) return;
    ranInitial.current = true;
    if (route.mode === "references") {
      if (route.symbol.trim()) void runReferences(route.symbol, route.branch ?? "");
    } else if (route.repo.trim() || route.target.trim()) {
      void runImports(route.repo, route.target, route.direction, route.branch ?? "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (mode === "references") {
      void runReferences(symbol, branch);
    } else {
      void runImports(repo, target, direction, branch);
    }
  }

  return (
    <div>
      <form className="search-box" onSubmit={handleSubmit}>
        {mode === "references" ? (
          <input
            type="text"
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="symbol name, e.g. process"
            aria-label="Symbol"
            autoFocus
          />
        ) : (
          <>
            <select value={direction} onChange={(e) => setDirection(e.target.value)} aria-label="Direction">
              <option value="imports">imports</option>
              <option value="imported_by">imported_by</option>
            </select>
            <input
              type="text"
              value={repo}
              onChange={(e) => setRepo(e.target.value)}
              placeholder="repo, e.g. acme/widgets"
              aria-label="Repo"
            />
            <input
              type="text"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder="target dotted path, e.g. os.path"
              aria-label="Target"
            />
          </>
        )}
        <input
          type="text"
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          placeholder="branch (optional)"
          aria-label="Branch"
        />
        <button type="submit">{mode === "references" ? "Find references" : "List imports"}</button>
      </form>
      <p className="result-summary">
        Candidate-set results, not compiler-precise references — name-resolved over raw{" "}
        {mode === "references" ? "call" : "import"} edges (grep-not-LSP); ambiguity is preserved,
        never collapsed to one answer.
      </p>
      {mode === "references"
        ? referencesBody(status, error, referencesEnvelope)
        : importsBody(status, error, importsEnvelope)}
    </div>
  );
}
