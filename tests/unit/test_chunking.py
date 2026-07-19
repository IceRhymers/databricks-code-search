"""Unit tests for indexer.parse.iter_chunks (issue #14 Phase 1)."""

from __future__ import annotations

import pytest

from indexer.languages import ParsedFile
from indexer.parse import iter_chunks


def _pf(content: str) -> ParsedFile:
    return ParsedFile(path="f.py", lang="python", size=len(content.encode()), content=content)


@pytest.mark.unit
def test_empty_file_yields_no_chunks() -> None:
    assert list(iter_chunks(_pf(""))) == []


@pytest.mark.unit
def test_short_file_is_a_single_chunk() -> None:
    content = "a = 1\nb = 2\n"
    chunks = list(iter_chunks(_pf(content), max_chars=2000))
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.chunk_index == 0
    assert chunk.content == content
    assert chunk.start_line == 1
    assert chunk.end_line == 2


@pytest.mark.unit
def test_respects_char_bound_and_splits_into_multiple_chunks() -> None:
    # 10 lines, each 10 chars incl. newline -> a max_chars of 25 fits ~2 lines/chunk.
    lines = [f"x{i:08d}\n" for i in range(10)]
    content = "".join(lines)
    chunks = list(iter_chunks(_pf(content), max_chars=25))
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.content) <= 25


@pytest.mark.unit
def test_chunk_index_is_monotonic_from_zero() -> None:
    lines = [f"x{i:08d}\n" for i in range(10)]
    chunks = list(iter_chunks(_pf("".join(lines)), max_chars=25))
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


@pytest.mark.unit
def test_line_ranges_are_1_based_contiguous_and_cover_the_file() -> None:
    lines = [f"x{i:08d}\n" for i in range(10)]
    chunks = list(iter_chunks(_pf("".join(lines)), max_chars=25))
    assert chunks[0].start_line == 1
    assert chunks[-1].end_line == 10
    for prev, nxt in zip(chunks, chunks[1:]):
        assert nxt.start_line == prev.end_line + 1


@pytest.mark.unit
def test_a_single_oversized_line_still_gets_its_own_chunk() -> None:
    # A line longer than max_chars is not split mid-line.
    content = "x" * 50 + "\n"
    chunks = list(iter_chunks(_pf(content), max_chars=10))
    assert len(chunks) == 1
    assert chunks[0].content == content


@pytest.mark.unit
def test_deterministic_across_repeated_calls() -> None:
    content = "".join(f"line {i}\n" for i in range(50))
    first = list(iter_chunks(_pf(content), max_chars=30))
    second = list(iter_chunks(_pf(content), max_chars=30))
    assert first == second


@pytest.mark.unit
def test_no_trailing_newline_is_still_covered() -> None:
    content = "line1\nline2"
    chunks = list(iter_chunks(_pf(content), max_chars=2000))
    assert len(chunks) == 1
    assert chunks[0].content == content
    assert chunks[0].end_line == 2
