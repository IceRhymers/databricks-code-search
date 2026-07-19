"""Walk an extracted repo tree and yield the text files worth indexing.

Every text file is stored (Phase 3 grep runs pg_trgm over ``content`` and ``path``
for all files), even unknown extensions (``lang=None``). Binary, oversized, and
``.git/`` contents are skipped.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from indexer.languages import (
    EXT_TO_LANG,
    MAX_FILE_BYTES,
    SEMANTIC_CHUNK_MAX_CHARS,
    Chunk,
    ParsedFile,
)

_BINARY_SNIFF_BYTES = 8192


def _looks_binary(data: bytes) -> bool:
    """A NUL byte in the first 8 KB is a strong, cheap binary signal."""
    return b"\x00" in data[:_BINARY_SNIFF_BYTES]


def iter_source_files(root: Path) -> Iterator[ParsedFile]:
    """Yield a :class:`ParsedFile` for each indexable text file under ``root``.

    Skips ``.git/``, files larger than ``MAX_FILE_BYTES``, and binary files (NUL
    sniff or UTF-8 decode failure). ``path`` is repo-relative (``root`` stripped).
    """
    root = root.resolve()
    for entry in sorted(root.rglob("*")):
        if not entry.is_file() or entry.is_symlink():
            continue
        if ".git" in entry.relative_to(root).parts:
            continue

        # Check size via stat() before read_bytes() so a huge asset file is
        # skipped without a full read into memory (peak memory stays bounded by
        # MAX_FILE_BYTES, not the largest file on disk).
        size = entry.stat().st_size
        if size > MAX_FILE_BYTES:
            continue

        raw = entry.read_bytes()
        if _looks_binary(raw):
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue

        rel_path = entry.relative_to(root).as_posix()
        lang = EXT_TO_LANG.get(entry.suffix.lower())
        yield ParsedFile(path=rel_path, lang=lang, size=size, content=content)


def iter_chunks(pf: ParsedFile, *, max_chars: int = SEMANTIC_CHUNK_MAX_CHARS) -> Iterator[Chunk]:
    """Split ``pf.content`` into line-aligned :class:`Chunk`\\ s bounded by ``max_chars``.

    Deterministic, line-based splitting (no tree-sitter, no overlap): lines are
    accumulated into the current chunk until the next line would push it past
    ``max_chars``, at which point the chunk is emitted and a new one starts. A
    single line longer than ``max_chars`` still gets its own chunk rather than
    being split mid-line. ``max_chars`` is a char-per-token approximation
    (~4 chars/token; see ``SEMANTIC_CHUNK_MAX_CHARS``), not an exact tokenizer
    count -- acceptable for V1 embedding-chunk sizing. ``chunk_index`` starts at
    0 and is monotonic; ``start_line``/``end_line`` are 1-based and inclusive.
    An empty file yields no chunks.
    """
    lines = pf.content.splitlines(keepends=True)
    if not lines:
        return

    chunk_index = 0
    buf: list[str] = []
    buf_chars = 0
    start_line = 1
    for lineno, line in enumerate(lines, start=1):
        if buf and buf_chars + len(line) > max_chars:
            yield Chunk(
                chunk_index=chunk_index,
                content="".join(buf),
                start_line=start_line,
                end_line=lineno - 1,
            )
            chunk_index += 1
            buf = []
            buf_chars = 0
            start_line = lineno
        buf.append(line)
        buf_chars += len(line)

    if buf:
        yield Chunk(
            chunk_index=chunk_index,
            content="".join(buf),
            start_line=start_line,
            end_line=len(lines),
        )
