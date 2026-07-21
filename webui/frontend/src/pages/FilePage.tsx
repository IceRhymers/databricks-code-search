import { useEffect, useState } from "react";
import { ApiError, getFile, type FileResponse } from "../api/client";
import { CodeBlock } from "../components/CodeBlock";
import { replaceRoute } from "../router";
import { locateNeedleLine } from "../utils/chunkAnchor";

function detectLang(path: string): string | null {
  const ext = path.split(".").pop()?.toLowerCase();
  const byExt: Record<string, string> = {
    py: "python",
    js: "javascript",
    jsx: "javascript",
    ts: "typescript",
    tsx: "tsx",
    go: "go",
    java: "java",
    rs: "rust",
  };
  return ext ? (byExt[ext] ?? null) : null;
}

type FilePageState =
  | { status: "loading" }
  | { status: "not_found" }
  | { status: "error"; message: string }
  | { status: "loaded"; file: FileResponse };

export function FilePage({
  repo,
  path,
  line,
  find,
}: {
  repo: string;
  path: string;
  line: number | null;
  find: string | null;
}): JSX.Element {
  const [state, setState] = useState<FilePageState>({ status: "loading" });
  const [copied, setCopied] = useState(false);
  // Lives in component state, not derived from the URL: the needle-hit rewrite below drops
  // the `find` param (replaceRoute lands on a plain #L<n> URL), and the fetch effect's deps
  // are [repo, path] so it won't refire when that happens -- a URL-derived note would vanish
  // the instant the rewrite occurs.
  const [anchorNote, setAnchorNote] = useState<string | null>(null);

  useEffect(() => {
    setState({ status: "loading" });
    setAnchorNote(null);
    getFile(repo, path)
      .then((file) => {
        setState({ status: "loaded", file });
        // `find` is the chunk's pre-computed needle (ChunkCard already ran extractNeedle);
        // only resolve it when the URL didn't already carry an explicit #L<n> anchor.
        if (find && line === null && file.content != null) {
          const located = locateNeedleLine(file.content, find);
          if (located === null) {
            setAnchorNote(
              "Couldn't locate the chunk in the current file content — content may have been re-indexed."
            );
            return;
          }
          const params = new URLSearchParams({ repo, path });
          replaceRoute(`/file?${params.toString()}#L${located.line}`);
          if (located.occurrences > 1) {
            setAnchorNote(
              `This line appears ${located.occurrences} times — showing the first occurrence, which may not be the chunk's exact location.`
            );
          }
        }
      })
      .catch((err) => {
        // /api/file 404s (not a 200 with found:false) on a miss -- see webui/main.py:api_file.
        if (err instanceof ApiError && err.status === 404) {
          setState({ status: "not_found" });
          return;
        }
        const message = err instanceof ApiError ? err.message : "Failed to load file.";
        setState({ status: "error", message });
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo, path]);

  function copyPermalink() {
    const url = new URL(window.location.href);
    void navigator.clipboard.writeText(url.toString()).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  if (state.status === "loading") {
    return <div className="result-summary">Loading…</div>;
  }
  if (state.status === "not_found") {
    return (
      <div className="banner warn">
        File not found: {repo}/{path}
      </div>
    );
  }
  if (state.status === "error") {
    return <div className="banner error">{state.message}</div>;
  }
  if (state.file.content == null) {
    return (
      <div className="banner warn">
        File not found: {repo}/{path}
      </div>
    );
  }

  return (
    <div>
      <div className="file-view-header">
        <h2>
          {repo}/{path}
        </h2>
        <span className="badge">{state.file.branch}</span>
        <button type="button" className="theme-toggle" onClick={copyPermalink}>
          {copied ? "Copied!" : "Copy permalink"}
        </button>
      </div>
      {anchorNote && (
        <div className="banner warn">
          {anchorNote}
          <button type="button" className="theme-toggle" onClick={() => setAnchorNote(null)}>
            Dismiss
          </button>
        </div>
      )}
      <CodeBlock content={state.file.content} lang={detectLang(path)} targetLine={line} />
    </div>
  );
}
