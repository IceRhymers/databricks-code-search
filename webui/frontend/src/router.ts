// Minimal client-side router (no dependency: this app only has three pages and deps should
// stay lean). The backend mounts SPAStaticFiles(html=True) at "/" so any deep link
// (e.g. /file?repo=x&path=y) falls back to index.html and hydrates here from location.

import { useEffect, useSyncExternalStore } from "react";

export type Route =
  | { page: "search"; query: string }
  | {
      page: "file";
      repo: string;
      path: string;
      line: number | null;
      endLine: number | null;
      find: string | null;
      branch: string | null;
    }
  | { page: "repos" }
  | { page: "semantic"; query: string }
  | { page: "graph"; mode: "references"; symbol: string; branch: string | null }
  | {
      page: "graph";
      mode: "imports";
      repo: string;
      target: string;
      // Verbatim string, NOT a union: an unknown value must flow through to the builder's
      // structured unsupported_direction 200 rather than being coerced/validated here.
      direction: string;
      branch: string | null;
    };

export function parseLocation(): Route {
  const { pathname, search, hash } = window.location;
  const params = new URLSearchParams(search);
  if (pathname === "/file") {
    // #L12 (single line) or #L12-L24 (inclusive range).
    const line = hash.match(/^#L(\d+)(?:-L(\d+))?$/);
    return {
      page: "file",
      repo: params.get("repo") ?? "",
      path: params.get("path") ?? "",
      line: line ? Number(line[1]) : null,
      endLine: line?.[2] ? Number(line[2]) : null,
      find: params.get("find"),
      branch: params.get("branch"),
    };
  }
  if (pathname === "/repos") {
    return { page: "repos" };
  }
  if (pathname === "/semantic") {
    return { page: "semantic", query: params.get("q") ?? "" };
  }
  if (pathname === "/references") {
    return {
      page: "graph",
      mode: "references",
      symbol: params.get("symbol") ?? "",
      branch: params.get("branch"),
    };
  }
  if (pathname === "/imports") {
    return {
      page: "graph",
      mode: "imports",
      repo: params.get("repo") ?? "",
      target: params.get("target") ?? "",
      direction: params.get("direction") ?? "imports",
      branch: params.get("branch"),
    };
  }
  return { page: "search", query: params.get("q") ?? "" };
}

let current = parseLocation();
const listeners = new Set<() => void>();

window.addEventListener("popstate", () => {
  current = parseLocation();
  listeners.forEach((l) => l());
});

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): Route {
  return current;
}

/** Navigate client-side (pushState) and notify subscribers. */
export function navigate(url: string): void {
  window.history.pushState(null, "", url);
  current = parseLocation();
  listeners.forEach((l) => l());
}

/** Replace the current entry (e.g. reflecting a submitted query into the URL). */
export function replaceRoute(url: string): void {
  window.history.replaceState(null, "", url);
  current = parseLocation();
  listeners.forEach((l) => l());
}

export function useRoute(): Route {
  return useSyncExternalStore(subscribe, getSnapshot);
}

/** Intercept same-origin left-clicks on <a href> so navigation stays client-side. */
export function useLinkInterception(): void {
  useEffect(() => {
    function onClick(event: MouseEvent) {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const anchor = (event.target as HTMLElement).closest("a");
      if (!anchor || anchor.target || anchor.hasAttribute("download")) return;
      const href = anchor.getAttribute("href");
      if (!href || !href.startsWith("/") || href.startsWith("//")) return;
      event.preventDefault();
      navigate(href);
    }
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);
}
