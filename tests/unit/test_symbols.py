"""Unit tests for indexer.symbols.extract_symbols across the V1 languages.

Covers nested symbols (a method inside a class) and the deliberate kind asymmetry:
Python has no distinct method node (``function_definition`` -> ``function`` even
inside a class) while JS/TS use ``method_definition`` -> ``method``.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from indexer.languages import ExtractedSymbol, ParsedFile
from indexer.symbols import _parser_for, extract_symbols

_CASES: dict[str, tuple[str, set[tuple[str, str]]]] = {
    "python": (
        "class C:\n    def m(self):\n        pass\ndef top():\n    pass\n",
        {("C", "class"), ("m", "function"), ("top", "function")},
    ),
    "javascript": (
        "class C {\n  m() {}\n}\nfunction top() {}\n",
        {("C", "class"), ("m", "method"), ("top", "function")},
    ),
    "typescript": (
        "interface I {}\nclass C {\n  m() {}\n}\nfunction top() {}\n",
        {("C", "class"), ("m", "method"), ("top", "function")},
    ),
    "go": (
        "package main\nfunc top() {}\nfunc (r R) m() {}\ntype T int\n",
        {("top", "function"), ("m", "method"), ("T", "type")},
    ),
    "java": (
        "class C {\n  void m() {}\n}\ninterface I {}\n",
        {("C", "class"), ("m", "method"), ("I", "interface")},
    ),
    "rust": (
        "fn top() {}\nstruct S;\nenum E {}\ntrait T {}\n",
        {("top", "function"), ("S", "struct"), ("E", "enum"), ("T", "trait")},
    ),
}


@pytest.mark.unit
@pytest.mark.parametrize("lang", sorted(_CASES))
def test_extract_symbols_per_language(lang: str) -> None:
    content, expected = _CASES[lang]
    pf = ParsedFile(path="x", lang=lang, size=len(content), content=content)
    got = {(s.name, s.kind) for s in extract_symbols(pf)}
    assert got == expected


@pytest.mark.unit
def test_nested_method_line_numbers() -> None:
    content = "class C:\n    def m(self):\n        pass\n"
    pf = ParsedFile(path="x", lang="python", size=len(content), content=content)
    by_name = {s.name: s for s in extract_symbols(pf)}
    assert by_name["C"] == ExtractedSymbol("C", "class", 1, 3)
    assert by_name["m"] == ExtractedSymbol("m", "function", 2, 3)


@pytest.mark.unit
def test_unsupported_language_returns_empty() -> None:
    assert extract_symbols(ParsedFile("x", None, 3, "hi\n")) == []
    assert extract_symbols(ParsedFile("x", "markdown", 3, "# hi\n")) == []


@pytest.mark.unit
def test_parser_cache_is_per_thread() -> None:
    """Each thread gets its own parser; within a thread the parser is reused.

    ``p1 is not p2`` is the load-bearing assertion: it guards the third-party
    contract that ``get_parser`` returns a *fresh* instance per call rather than
    a shared singleton, which is what makes the thread-local cache meaningful.

    Uses explicit threads, not a pool: a pool may serve both calls from one
    worker thread, in which case the thread-local correctly returns the same
    parser and the assertion would fail for the wrong reason.
    """
    got: dict[int, Any] = {}
    barrier = threading.Barrier(2)

    def grab(idx: int) -> None:
        barrier.wait()
        got[idx] = _parser_for("python")

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    p1, p2 = got[0], got[1]
    assert p1 is not p2

    same: list[Any] = []

    def twice() -> None:
        same.extend((_parser_for("python"), _parser_for("python")))

    t = threading.Thread(target=twice)
    t.start()
    t.join()
    assert same[0] is same[1]


@pytest.mark.unit
def test_concurrent_extraction_matches_serial() -> None:
    """Two threads extracting from different sources agree with serial results."""
    sources = [_CASES["python"][0], "def only():\n    return 1\n"]
    files = [
        ParsedFile(path=f"f{i}", lang="python", size=len(s), content=s)
        for i, s in enumerate(sources)
    ]
    expected = [extract_symbols(pf) for pf in files]

    iterations = 200
    barrier = threading.Barrier(len(files))

    def run(idx: int) -> bool:
        barrier.wait()
        return all(extract_symbols(files[idx]) == expected[idx] for _ in range(iterations))

    with ThreadPoolExecutor(max_workers=len(files)) as pool:
        results = [f.result() for f in [pool.submit(run, i) for i in range(len(files))]]

    assert all(results)
