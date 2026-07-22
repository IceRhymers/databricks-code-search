// A single-pass "is this a flat AND of atoms?" recognizer for the zoekt-style query
// language the backend parses (app/query/parser.py). This is NOT a full parser -- it
// mirrors just enough of app/query/parser.py:tokenize (lines 249-300) to classify a query
// as either "safe" (a flat conjunction of field:value / bareword atoms the chip UI can
// edit structurally) or "unsafe" (contains parens, OR, quotes, regex, or negation ('-') --
// anything whose structure a naive atom-rewrite could silently corrupt). Unsafe queries are
// still valid zoekt queries; they are just left as opaque free text for the chips to disable
// rather than edit. See docs/runbooks/webui.md for the user-facing asymmetry this produces.
import type { RepoInfo } from "../api/client";

export type AtomField = "repo" | "file" | "lang" | "sym" | "branch" | "case" | "commit";

export interface Atom {
  /** null means an untyped content atom (a bareword substring). */
  field: AtomField | null;
  value: string;
  /** Raw source span [start, end) this atom occupies -- used to rebuild edited queries. */
  start: number;
  end: number;
}

export type QueryModel = { safe: false } | { safe: true; source: string; atoms: Atom[] };

const BAREWORD_STOP = new Set([" ", "\t", "\n", "(", ")"]);
const SUPPORTED_FIELDS: Record<string, AtomField> = {
  repo: "repo",
  file: "file",
  lang: "lang",
  sym: "sym",
  branch: "branch",
  commit: "commit",
};
const RESERVED_FIELDS = new Set(["content", "r", "f", "l", "b", "c", "s"]);
// "case" is a recognized prefix but not a SUPPORTED_FIELDS entry (it carries a query-global
// flag, not a filter value) -- it needs its own membership check alongside SUPPORTED_FIELDS.
const RECOGNIZED_PREFIXES = new Set([...Object.keys(SUPPORTED_FIELDS), "case"]);

function isWhitespace(ch: string): boolean {
  return ch === " " || ch === "\t" || ch === "\n";
}

function isLowerAscii(ch: string): boolean {
  return ch >= "a" && ch <= "z";
}

/** Read a bare (unquoted, non-regex) value from `start` until a stop char or EOF. */
function readBareValue(source: string, start: number): [string, number] {
  const n = source.length;
  let j = start;
  while (j < n && !BAREWORD_STOP.has(source[j])) j++;
  return [source.slice(start, j), j];
}

/**
 * Classify `query` as a flat AND of atoms ("safe") or bail ("unsafe") on the first
 * paren, OR, quote, regex, or negation ('-') delimiter -- mirroring
 * app/query/parser.py:tokenize's scan but refusing (rather than parsing) anything with real
 * boolean/grouping/negation structure.
 */
export function recognize(query: string): QueryModel {
  const atoms: Atom[] = [];
  const n = query.length;
  let i = 0;

  while (i < n) {
    const c = query[i];
    if (isWhitespace(c)) {
      i += 1;
      continue;
    }
    // Parens anywhere mean real grouping structure -- unsafe to rewrite as flat atoms.
    if (c === "(" || c === ")") return { safe: false };
    // A regex or quoted literal in operand position -- unsafe (chips never edit these).
    if (c === "/" || c === '"') return { safe: false };
    // A token-initial '-' is lexical negation on the Python side (parses to a Not(...) node),
    // whose structure a flat atom-rewrite cannot represent -- unsafe. Mirror the Python
    // scanner's exact trigger: the next char must exist, be non-whitespace, and not be ')'.
    // Any other '-' (at EOF, or before whitespace/')' ) is a literal and falls through. This
    // runs at every token start (not just i === 0), so a leading '-' on ANY operand triggers it.
    if (c === "-" && i + 1 < n && !isWhitespace(query[i + 1]) && query[i + 1] !== ")") {
      return { safe: false };
    }

    const start = i;
    // Mirror tokenize's field-prefix scan: leading [a-z]+ followed by ':'.
    let k = i;
    while (k < n && isLowerAscii(query[k])) k++;
    if (k > i && k < n && query[k] === ":") {
      const prefix = query.slice(i, k);
      if (RECOGNIZED_PREFIXES.has(prefix) || RESERVED_FIELDS.has(prefix)) {
        if (RESERVED_FIELDS.has(prefix)) return { safe: false };
        const valueStart = k + 1;
        if (valueStart < n && (query[valueStart] === '"' || query[valueStart] === "/")) {
          return { safe: false }; // quoted/regex field value -- unsafe
        }
        const [value, end] = readBareValue(query, valueStart);
        if (value === "") return { safe: false }; // empty field value
        if (prefix === "case") {
          if (value !== "yes" && value !== "no") return { safe: false };
          atoms.push({ field: "case", value, start, end });
        } else {
          atoms.push({ field: SUPPORTED_FIELDS[prefix], value, start, end });
        }
        i = end;
        continue;
      }
    }

    // Not a recognized field prefix: read the whole raw bareword.
    const [lexeme, end] = readBareValue(query, i);
    i = end;
    if (lexeme.toLowerCase() === "or") return { safe: false };
    atoms.push({ field: null, value: lexeme, start, end });
  }

  return { safe: true, source: query, atoms };
}

export interface BranchChipState {
  available: boolean;
  options: string[];
  active: string | null;
}

export interface DerivedChips {
  /** false for an unsafe model -- repo/lang chips render disabled, branch chips hide. */
  editable: boolean;
  repoActive: string | null;
  langActive: string | null;
  branch: BranchChipState;
}

const EMPTY_BRANCH: BranchChipState = { available: false, options: [], active: null };

/**
 * Derive chip state from a recognized model. Branch chips only ever appear for a query
 * that names exactly one known repository (never a union across repos, and never a
 * regex/glob repo pattern) with at most one existing branch atom -- see
 * docs/runbooks/webui.md for why this is deliberately narrower than repo/lang chips.
 */
export function deriveChips(model: QueryModel, repos: RepoInfo[]): DerivedChips {
  if (!model.safe) {
    return { editable: false, repoActive: null, langActive: null, branch: EMPTY_BRANCH };
  }

  const repoAtoms = model.atoms.filter((a) => a.field === "repo");
  const langAtoms = model.atoms.filter((a) => a.field === "lang");
  const branchAtoms = model.atoms.filter((a) => a.field === "branch");

  const repoActive = repoAtoms.length === 1 ? repoAtoms[0].value : null;
  const langActive = langAtoms.length === 1 ? langAtoms[0].value : null;

  let branch: BranchChipState = EMPTY_BRANCH;
  if (repoAtoms.length === 1 && branchAtoms.length <= 1) {
    const repo = repos.find((r) => r.name === repoAtoms[0].value);
    if (repo) {
      branch = {
        available: true,
        options: repo.branches,
        active: branchAtoms.length === 1 ? branchAtoms[0].value : null,
      };
    }
  }

  return { editable: true, repoActive, langActive, branch };
}

/** bare-safe: renders without quoting. First char must not need delimiter escaping, and
 * the value must contain no whitespace/parens/quotes anywhere (a mid-value '/' is fine). */
function isBareSafe(value: string): boolean {
  if (value.length === 0) return false;
  const first = value[0];
  if (first === "(" || first === ")" || first === '"' || first === "/") return false;
  for (const ch of value) {
    if (isWhitespace(ch) || ch === "(" || ch === ")" || ch === '"') return false;
  }
  return true;
}

function formatAtomValue(value: string): string {
  if (isBareSafe(value)) return value;
  return `"${value.replace(/"/g, '\\"')}"`;
}

/** Rebuild a query string from the atoms passing `keep`, in original order and raw text,
 * plus an optional appended `field:value` atom. */
function rebuild(model: { source: string; atoms: Atom[] }, keep: (atom: Atom) => boolean, appended: string | null): string {
  const parts = model.atoms.filter(keep).map((a) => model.source.slice(a.start, a.end));
  if (appended !== null) parts.push(appended);
  return parts.join(" ");
}

/** Remove every atom of `field`, then append `field:value` (formatted) unless `value` is
 * null, in which case the field is simply cleared. */
export function setFieldAtom(model: { source: string; atoms: Atom[] }, field: AtomField, value: string | null): string {
  const appended = value === null ? null : `${field}:${formatAtomValue(value)}`;
  return rebuild(model, (a) => a.field !== field, appended);
}

/** Toggle a single `branch:` atom: drop it if it is already the sole branch atom with this
 * value, else replace all branch atoms with `branch:value`. */
export function toggleBranchAtom(model: { source: string; atoms: Atom[] }, value: string): string {
  const branchAtoms = model.atoms.filter((a) => a.field === "branch");
  const isActive = branchAtoms.length === 1 && branchAtoms[0].value === value;
  return setFieldAtom(model, "branch", isActive ? null : value);
}
