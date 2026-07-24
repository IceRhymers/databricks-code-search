import type { GraphCandidate, ReferenceSite } from "../api/client";

// Shared candidate-set renderer for /references and /imports: both `_site_payload` shapes are
// identical (app/service.py), so one component covers both GraphPage modes.

function fileHref(repo: string, path: string, line: number | null, branch: string | null): string {
  const params = new URLSearchParams({ repo, path });
  if (branch) params.set("branch", branch);
  const anchor = line != null ? `#L${line}` : "";
  return `/file?${params.toString()}${anchor}`;
}

function SignalChips({ candidate }: { candidate: GraphCandidate }): JSX.Element | null {
  const chips: string[] = [];
  if (candidate.same_repo) chips.push("same repo");
  if (candidate.same_file) chips.push("same file");
  if (candidate.kind_match) chips.push("kind match");
  if (chips.length === 0) return null;
  return (
    <span className="signal-chips">
      {chips.map((chip) => (
        <span key={chip} className="chip">
          {chip}
        </span>
      ))}
    </span>
  );
}

function CandidateRow({ candidate, branch }: { candidate: GraphCandidate; branch: string | null }): JSX.Element {
  return (
    <li className="candidate-row">
      <a href={fileHref(candidate.repo, candidate.file, candidate.line, branch)}>
        {candidate.name} <span className="lang">({candidate.kind})</span>
      </a>
      <span className="candidate-location">
        {candidate.repo}/{candidate.file}:{candidate.line}
      </span>
      <SignalChips candidate={candidate} />
    </li>
  );
}

function SiteCard({ site, branch }: { site: ReferenceSite; branch: string | null }): JSX.Element {
  const enclosing = site.enclosing_symbol
    ? `${site.enclosing_symbol.name} (${site.enclosing_symbol.kind})`
    : "module scope";
  return (
    <div className="result-file">
      <div className="result-file-header">
        <a href={fileHref(site.repo, site.file, site.line, branch)}>
          {site.repo}/{site.file}:{site.line}
        </a>
        <span className={`badge resolution-${site.resolution}`}>{site.resolution}</span>
      </div>
      <div className="result-summary">
        <code>{site.target_name}</code> in {enclosing}
      </div>
      {site.candidates.length > 0 && (
        <ul className="candidate-list">
          {site.candidates.map((candidate, i) => (
            <CandidateRow key={i} candidate={candidate} branch={branch} />
          ))}
        </ul>
      )}
      {site.candidates_truncated && (
        <div className="result-summary">
          showing {site.candidates.length} of {site.candidate_count} candidates
        </div>
      )}
    </div>
  );
}

export function SiteList({
  sites,
  siteCount,
  resolutionSummary,
  truncated,
  truncationReason,
  branch,
  emptyMessage,
}: {
  sites: ReferenceSite[];
  siteCount: number;
  resolutionSummary: { unique: number; ambiguous: number; unresolved: number };
  truncated: boolean;
  truncationReason: string | null;
  branch: string | null;
  emptyMessage: string;
}): JSX.Element {
  return (
    <div>
      <div className="result-summary">
        {siteCount} site{siteCount === 1 ? "" : "s"} — {resolutionSummary.unique} unique,{" "}
        {resolutionSummary.ambiguous} ambiguous, {resolutionSummary.unresolved} unresolved
      </div>
      {truncated && (
        <div className="banner warn">
          Results truncated{truncationReason ? ` (${truncationReason})` : ""}.
        </div>
      )}
      {sites.length === 0 ? (
        <div className="result-summary">{emptyMessage}</div>
      ) : (
        sites.map((site, i) => <SiteCard key={i} site={site} branch={branch} />)
      )}
    </div>
  );
}
