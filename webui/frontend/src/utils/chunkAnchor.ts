// Locates a semantic search chunk's position within a file's full content. FALLBACK path
// (issue #36): chunks indexed since issue #44 carry an exact start_line/end_line and
// ChunkCard links straight to #L<start>-L<end>; rows indexed before line tracking have
// nulls, so ChunkCard picks a "needle" line from the chunk and FilePage re-finds that same
// text in the file it loads -- an approximate but good-enough anchor.

/**
 * The anchor needle for a chunk: its LONGEST non-empty trimmed line (not the first --
 * first lines of token-cut chunks are generic and cause silent wrong-line hits).
 * Returns null for a whitespace-only chunk.
 */
export function extractNeedle(chunkContent: string): string | null {
  let longest: string | null = null;
  for (const rawLine of chunkContent.split("\n")) {
    const line = rawLine.trim();
    if (line.length > 0 && (longest === null || line.length > longest.length)) {
      longest = line;
    }
  }
  return longest;
}

/**
 * Locate `needle` in the file's content: 1-based line of the FIRST occurrence plus the
 * total occurrence count (>1 means the jump is approximate), or null when absent.
 */
export function locateNeedleLine(fileContent: string, needle: string): { line: number; occurrences: number } | null {
  const idx = fileContent.indexOf(needle);
  if (idx === -1) return null;

  const line = fileContent.slice(0, idx).split("\n").length;

  let occurrences = 1;
  let next = fileContent.indexOf(needle, idx + 1);
  while (next !== -1) {
    occurrences += 1;
    next = fileContent.indexOf(needle, next + 1);
  }

  return { line, occurrences };
}
