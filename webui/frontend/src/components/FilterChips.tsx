import type { RepoInfo } from "../api/client";
import type { DerivedChips } from "../utils/queryModel";

export interface FilterChipsProps {
  repos: RepoInfo[];
  languages: string[];
  chips: DerivedChips;
  onToggleRepo: (name: string) => void;
  onToggleLanguage: (lang: string) => void;
  onToggleBranch: (branch: string) => void;
}

const UNSAFE_TITLE = "This query has OR/parens/quotes/regex -- edit the text directly.";

// Chips compose `repo:`/`lang:`/`branch:` atoms into the query string (owned by SearchPage,
// via utils/queryModel's recognizer); they never call the API directly. `languages` is
// derived from the languages seen in the current result set rather than a separate endpoint,
// keeping this lean (no extra backend surface for v1).
//
// Deliberate asymmetry: repo/lang chips stay visible-but-disabled for an unsafe query (so the
// filter set doesn't visually vanish), while the branch group hides entirely whenever it isn't
// unambiguous (unsafe query, zero or multiple repo atoms, or multiple branch atoms already
// present) -- see docs/runbooks/webui.md. Branch options are always exactly one repo's
// branches, never a union across repos.
export function FilterChips({
  repos,
  languages,
  chips,
  onToggleRepo,
  onToggleLanguage,
  onToggleBranch,
}: FilterChipsProps): JSX.Element | null {
  if (repos.length === 0 && languages.length === 0) return null;
  const disabled = !chips.editable;
  return (
    <div className="chips">
      {repos.map((repo) => (
        <button
          key={`repo-${repo.name}`}
          type="button"
          className={`chip${chips.repoActive === repo.name ? " active" : ""}`}
          onClick={() => onToggleRepo(repo.name)}
          disabled={disabled}
          title={disabled ? UNSAFE_TITLE : undefined}
        >
          repo:{repo.name}
        </button>
      ))}
      {languages.map((lang) => (
        <button
          key={`lang-${lang}`}
          type="button"
          className={`chip${chips.langActive === lang ? " active" : ""}`}
          onClick={() => onToggleLanguage(lang)}
          disabled={disabled}
          title={disabled ? UNSAFE_TITLE : undefined}
        >
          lang:{lang}
        </button>
      ))}
      {chips.branch.available &&
        chips.branch.options.map((branch) => (
          <button
            key={`branch-${branch}`}
            type="button"
            className={`chip${chips.branch.active === branch ? " active" : ""}`}
            onClick={() => onToggleBranch(branch)}
          >
            branch:{branch}
          </button>
        ))}
    </div>
  );
}
