import { useEffect, useRef, useState } from "react";
import type { HighlighterCore, LanguageInput } from "shiki/core";
import { useTheme } from "../theme";

// Indexed languages (indexer/languages.py EXT_TO_LANG) are exactly: python, javascript,
// typescript, tsx, go, java, rust -- every one is already a valid Shiki bundled-language id,
// so no alias map is needed. Anything else (lang is null, or an id Shiki doesn't recognize)
// falls back to plain text rather than throwing.
//
// Importing the top-level `shiki` package pulls in its full ~80-language bundle (Vite cannot
// tree-shake a dynamic `bundledLanguages[key]` lookup, since the key is a runtime value) --
// several MB even though this corpus only ever needs 7 of them. Using the fine-grained
// `shiki/core` API with static per-language imports lets the bundler code-split each language
// (and both themes) into its own small chunk, fetched only the first time a file view actually
// needs it. The JS regex engine (not `shiki/engine/oniguruma`) avoids shipping/streaming a wasm
// binary for a static app deployment.
const SHIKI_THEME_LIGHT = "github-light";
const SHIKI_THEME_DARK = "github-dark";

const LANG_LOADERS: Record<string, () => LanguageInput> = {
  python: () => import("shiki/langs/python.mjs"),
  javascript: () => import("shiki/langs/javascript.mjs"),
  typescript: () => import("shiki/langs/typescript.mjs"),
  tsx: () => import("shiki/langs/tsx.mjs"),
  go: () => import("shiki/langs/go.mjs"),
  java: () => import("shiki/langs/java.mjs"),
  rust: () => import("shiki/langs/rust.mjs"),
};

interface Token {
  content: string;
  color?: string;
}

let highlighterPromise: Promise<HighlighterCore> | null = null;

/** Build the highlighter once (module singleton), loaded only on first use. */
function getHighlighter(): Promise<HighlighterCore> {
  if (!highlighterPromise) {
    highlighterPromise = (async () => {
      const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] = await Promise.all([
        import("shiki/core"),
        import("shiki/engine/javascript"),
      ]);
      return createHighlighterCore({
        themes: [import("shiki/themes/github-light.mjs"), import("shiki/themes/github-dark.mjs")],
        langs: Object.values(LANG_LOADERS).map((load) => load()),
        engine: createJavaScriptRegexEngine(),
      });
    })();
  }
  return highlighterPromise;
}

async function tokenizeLines(code: string, lang: string | null, theme: string): Promise<Token[][] | null> {
  try {
    const highlighter = await getHighlighter();
    const resolvedLang = lang && lang in LANG_LOADERS ? lang : "text";
    if (resolvedLang !== "text" && !highlighter.getLoadedLanguages().includes(resolvedLang)) {
      await highlighter.loadLanguage(LANG_LOADERS[resolvedLang]());
    }
    const result = highlighter.codeToTokens(code, { lang: resolvedLang, theme });
    return result.tokens.map((line) => line.map((t) => ({ content: t.content, color: t.color })));
  } catch {
    return null; // syntax highlighting is a progressive enhancement; plain text is a fine fallback
  }
}

export function CodeBlock({
  content,
  lang,
  targetLine,
  targetEndLine = null,
}: {
  content: string;
  lang: string | null;
  targetLine: number | null;
  /** Inclusive end of a highlighted range (issue #44 chunk anchors); null = single line. */
  targetEndLine?: number | null;
}): JSX.Element {
  const theme = useTheme();
  const [tokenLines, setTokenLines] = useState<Token[][] | null>(null);
  const targetRef = useRef<HTMLDivElement | null>(null);
  const shikiTheme = theme === "dark" ? SHIKI_THEME_DARK : SHIKI_THEME_LIGHT;

  useEffect(() => {
    let cancelled = false;
    setTokenLines(null);
    void tokenizeLines(content, lang, shikiTheme).then((result) => {
      if (!cancelled) setTokenLines(result);
    });
    return () => {
      cancelled = true;
    };
  }, [content, lang, shikiTheme]);

  useEffect(() => {
    targetRef.current?.scrollIntoView({ block: "center" });
  }, [tokenLines, targetLine]);

  const plainLines = content.split("\n");
  const lines: Token[][] = tokenLines ?? plainLines.map((text): Token[] => [{ content: text }]);

  return (
    <div className="code-view">
      <pre>
        {lines.map((lineTokens, i) => {
          const lineNo = i + 1;
          const isTarget =
            targetLine !== null && lineNo >= targetLine && lineNo <= (targetEndLine ?? targetLine);
          return (
            <div
              key={lineNo}
              id={`L${lineNo}`}
              ref={lineNo === targetLine ? targetRef : undefined}
              className={`code-line${isTarget ? " target" : ""}`}
            >
              <a className="line-no" href={`#L${lineNo}`}>
                {lineNo}
              </a>
              <span className="line-text">
                {lineTokens.map((t, j) => (
                  <span key={j} style={t.color ? { color: t.color } : undefined}>
                    {t.content}
                  </span>
                ))}
              </span>
            </div>
          );
        })}
      </pre>
    </div>
  );
}
