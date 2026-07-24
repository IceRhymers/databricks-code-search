import { useEffect, useState } from "react";
import { getSemanticStatus } from "./api/client";
import { ThemeToggle } from "./components/ThemeToggle";
import { FilePage } from "./pages/FilePage";
import { GraphPage } from "./pages/GraphPage";
import { ReposPage } from "./pages/ReposPage";
import { SearchPage } from "./pages/SearchPage";
import { SemanticPage } from "./pages/SemanticPage";
import { useLinkInterception, useRoute } from "./router";

export function App(): JSX.Element {
  useLinkInterception();
  const route = useRoute();
  // Fail-closed: hidden until the status probe proves the flag is on; a failed fetch keeps
  // it hidden rather than risk showing a dead tab.
  const [semanticEnabled, setSemanticEnabled] = useState(false);

  useEffect(() => {
    getSemanticStatus()
      .then((status) => setSemanticEnabled(status.semantic_enabled))
      .catch(() => setSemanticEnabled(false));
  }, []);

  return (
    <div className="app-shell">
      <header className="app-header">
        <span className="brand">Code Search</span>
        <nav>
          <a href="/">Search</a>
          {semanticEnabled && <a href="/semantic">Semantic</a>}
          <a href="/references">Graph</a>
          <a href="/repos">Repos</a>
        </nav>
        <ThemeToggle />
      </header>
      <main className="app-main">
        {route.page === "search" && <SearchPage initialQuery={route.query} />}
        {route.page === "file" && (
          <FilePage
            repo={route.repo}
            path={route.path}
            line={route.line}
            endLine={route.endLine}
            find={route.find}
            branch={route.branch}
          />
        )}
        {route.page === "repos" && <ReposPage />}
        {route.page === "semantic" && <SemanticPage initialQuery={route.query} />}
        {route.page === "graph" && <GraphPage key={route.mode} route={route} />}
      </main>
    </div>
  );
}
