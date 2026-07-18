"""Unit tests for indexer.symbols.extract_symbols across the V1 languages.

Covers nested symbols (a method inside a class) and the deliberate kind asymmetry:
Python has no distinct method node (``function_definition`` -> ``function`` even
inside a class) while JS/TS use ``method_definition`` -> ``method``.
"""

from __future__ import annotations

import pytest

from indexer.languages import ExtractedSymbol, ParsedFile
from indexer.symbols import extract_symbols

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
