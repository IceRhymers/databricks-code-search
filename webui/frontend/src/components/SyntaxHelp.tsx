// Collapsible reference for the zoekt-style query grammar the backend understands
// (app/query/parser.py). Kept as static markup -- the grammar is a documented, stable surface
// and this is the only place a webui user can discover it without reading the MCP tool docs.
export function SyntaxHelp(): JSX.Element {
  return (
    <details className="syntax-help">
      <summary>Query syntax</summary>
      <table>
        <tbody>
          <tr>
            <td>
              <code>repo:name</code>
            </td>
            <td>Limit to a repository (see the Repos page for names).</td>
          </tr>
          <tr>
            <td>
              <code>file:pattern</code>
            </td>
            <td>Limit by file path glob/substring, e.g. <code>file:*.py</code>.</td>
          </tr>
          <tr>
            <td>
              <code>lang:go</code>
            </td>
            <td>Limit by detected language.</td>
          </tr>
          <tr>
            <td>
              <code>sym:Name</code>
            </td>
            <td>Match a symbol definition (function, class, etc.) by name.</td>
          </tr>
          <tr>
            <td>
              <code>branch:name</code>
            </td>
            <td>
              Limit to files present on a branch (exact membership, not a glob or regex).
              Omitted, search covers each repo&apos;s default branch.
            </td>
          </tr>
          <tr>
            <td>
              <code>case:yes</code>
            </td>
            <td>Case-sensitive match (default is case-insensitive).</td>
          </tr>
          <tr>
            <td>
              <code>-term</code>
            </td>
            <td>
              Exclude (negate) the next atom, e.g. <code>-repo:acme</code> or{" "}
              <code>-lang:go</code>. Binds tighter than AND; only a leading{" "}
              <code>-</code> immediately before a non-space, non-<code>)</code> character
              negates -- a trailing or standalone <code>-</code> (as in{" "}
              <code>foo -</code>) stays a literal dash. Quote it to search for a literal
              leading dash, e.g. <code>&quot;-foo&quot;</code>. Not supported in semantic
              search.
            </td>
          </tr>
          <tr>
            <td>
              <code>foo bar</code>
            </td>
            <td>Space between atoms is AND.</td>
          </tr>
          <tr>
            <td>
              <code>foo or bar</code>
            </td>
            <td>Boolean OR between atoms.</td>
          </tr>
          <tr>
            <td>
              <code>/regex/</code>
            </td>
            <td>Regular expression content match.</td>
          </tr>
          <tr>
            <td>
              <code>&quot;exact phrase&quot;</code>
            </td>
            <td>Quote a phrase containing spaces or special characters.</td>
          </tr>
        </tbody>
      </table>
    </details>
  );
}
