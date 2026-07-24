import { useEffect, useState } from "react";
import { ApiError, listRepos, type RepoInfo } from "../api/client";

export function ReposPage(): JSX.Element {
  const [state, setState] = useState<
    { status: "loading" } | { status: "error"; message: string } | { status: "loaded"; repos: RepoInfo[] }
  >({ status: "loading" });

  useEffect(() => {
    listRepos()
      .then((res) => setState({ status: "loaded", repos: res.repos }))
      .catch((err) => {
        const message = err instanceof ApiError ? err.message : "Failed to load repositories.";
        setState({ status: "error", message });
      });
  }, []);

  if (state.status === "loading") {
    return <div className="result-summary">Loading…</div>;
  }
  if (state.status === "error") {
    return <div className="banner error">{state.message}</div>;
  }

  return (
    <table className="repos-table">
      <thead>
        <tr>
          <th>Repository</th>
          <th>Default branch</th>
          <th>Last indexed commit</th>
          <th>Last indexed at</th>
        </tr>
      </thead>
      <tbody>
        {state.repos.map((repo) => (
          <tr key={repo.name}>
            <td>
              <a href={`/?q=${encodeURIComponent(`repo:${repo.name} `)}`}>{repo.name}</a>{" "}
              <a href={`/imports?repo=${encodeURIComponent(repo.name)}&direction=imports`}>imports</a>
            </td>
            <td>{repo.default_branch ?? "—"}</td>
            <td>{repo.last_indexed_commit ? repo.last_indexed_commit.slice(0, 12) : "—"}</td>
            <td>{repo.index_time ? new Date(repo.index_time).toLocaleString() : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
