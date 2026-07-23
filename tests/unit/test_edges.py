"""Unit tests for indexer.symbols.extract_file's reference-edge extraction (Python, #84).

Mirrors test_symbols.py's style: pure tree-sitter parsing, no DB. Covers call-target
resolution (D4), import-target resolution (D5), and enclosing attribution (D6).
"""

from __future__ import annotations

import pytest

from indexer.languages import ExtractedEdge, ParsedFile
from indexer.symbols import extract_file, extract_symbols


def _pf(content: str, lang: str | None = "python") -> ParsedFile:
    return ParsedFile(path="x.py", lang=lang, size=len(content), content=content)


def _edges(content: str) -> list[ExtractedEdge]:
    return extract_file(_pf(content)).edges


@pytest.mark.unit
def test_bare_call_at_top_level() -> None:
    edges = _edges("f(x)\n")
    assert edges == [ExtractedEdge(kind="call", target="f", line=1, enclosing=None)]


@pytest.mark.unit
def test_nested_calls_two_edges_correct_lines() -> None:
    edges = _edges("f(g(x))\n")
    assert [(e.target, e.line) for e in edges] == [("f", 1), ("g", 1)]


@pytest.mark.unit
def test_method_and_bare_calls_both_use_rightmost_identifier() -> None:
    edges = _edges("self.helper()\nhelper()\n")
    assert [e.target for e in edges] == ["helper", "helper"]


@pytest.mark.unit
def test_dotted_callee_uses_rightmost_identifier() -> None:
    edges = _edges("a.b.f()\n")
    assert [e.target for e in edges] == ["f"]


@pytest.mark.unit
def test_enclosing_attribution_function_method_class_and_module() -> None:
    content = (
        "def top():\n"
        "    call_in_fn()\n"
        "\n"
        "class C:\n"
        "    def m(self):\n"
        "        call_in_method()\n"
        "    call_in_class_body()\n"
        "\n"
        "call_at_module_scope()\n"
    )
    fx = extract_file(_pf(content))
    by_target = {e.target: e for e in fx.edges}

    fn_edge = by_target["call_in_fn"]
    assert fn_edge.enclosing is not None
    assert fn_edge.enclosing.name == "top"
    assert fn_edge.enclosing.kind == "function"

    method_edge = by_target["call_in_method"]
    assert method_edge.enclosing is not None
    assert method_edge.enclosing.name == "m"
    assert method_edge.enclosing.kind == "function"

    class_edge = by_target["call_in_class_body"]
    assert class_edge.enclosing is not None
    assert class_edge.enclosing.name == "C"
    assert class_edge.enclosing.kind == "class"

    module_edge = by_target["call_at_module_scope"]
    assert module_edge.enclosing is None

    by_name = {s.name: s for s in fx.symbols}
    assert method_edge.enclosing.start_line == by_name["m"].start_line
    assert method_edge.enclosing.end_line == by_name["m"].end_line
    assert class_edge.enclosing.start_line == by_name["C"].start_line
    assert class_edge.enclosing.end_line == by_name["C"].end_line


@pytest.mark.unit
def test_decorator_with_args_emits_edge_bare_decorator_does_not() -> None:
    content = "@deco(x)\ndef foo(): pass\n@bare\ndef bar(): pass\n"
    edges = _edges(content)
    assert [e.target for e in edges] == ["deco"]


@pytest.mark.unit
def test_non_identifier_callees_are_skipped() -> None:
    edges = _edges("xs[0]()\nf()()\n")
    # xs[0]() has no rightmost identifier -> skipped.
    # f()()'s outer call target is itself a `call` node -> skipped; the inner f() is counted once.
    assert [e.target for e in edges] == ["f"]


@pytest.mark.unit
def test_import_plain_dotted_path() -> None:
    edges = _edges("import a.b.c\n")
    assert edges == [ExtractedEdge(kind="import", target="a.b.c", line=1, enclosing=None)]


@pytest.mark.unit
def test_import_alias_is_insensitive_to_binding_name() -> None:
    edges = _edges("import a.b.c as d\n")
    assert [e.target for e in edges] == ["a.b.c"]


@pytest.mark.unit
def test_import_multiple_names_two_edges() -> None:
    edges = _edges("import a, b\n")
    assert [e.target for e in edges] == ["a", "b"]


@pytest.mark.unit
def test_from_import_names_and_alias() -> None:
    edges = _edges("from a.b import c, d as e\n")
    assert [e.target for e in edges] == ["a.b.c", "a.b.d"]


@pytest.mark.unit
def test_relative_import_single_dot() -> None:
    edges = _edges("from . import x\n")
    assert [e.target for e in edges] == [".x"]


@pytest.mark.unit
def test_relative_import_double_dot_with_module() -> None:
    edges = _edges("from ..p import q\n")
    assert [e.target for e in edges] == ["..p.q"]


@pytest.mark.unit
def test_wildcard_import_targets_the_module() -> None:
    edges = _edges("from a.b import *\n")
    assert edges == [ExtractedEdge(kind="import", target="a.b", line=1, enclosing=None)]


@pytest.mark.unit
def test_multiline_parenthesized_from_import_per_name_lines() -> None:
    content = "from a.b import (\n    c,\n    d,\n)\n"
    edges = _edges(content)
    assert [(e.target, e.line) for e in edges] == [("a.b.c", 2), ("a.b.d", 3)]


@pytest.mark.unit
def test_function_local_import_attributes_to_enclosing_function() -> None:
    content = "def outer():\n    import os\n"
    fx = extract_file(_pf(content))
    assert len(fx.edges) == 1
    edge = fx.edges[0]
    assert edge.target == "os"
    assert edge.enclosing is not None
    assert edge.enclosing.name == "outer"


@pytest.mark.unit
def test_non_python_languages_and_none_lang_yield_no_edges() -> None:
    js_content = "f(x);\nimport { a } from 'b';\n"
    assert extract_file(_pf(js_content, lang="javascript")).edges == []
    assert extract_file(_pf("f(x)\n", lang=None)).edges == []


@pytest.mark.unit
def test_extract_symbols_is_a_thin_wrapper_over_extract_file() -> None:
    content = "class C:\n    def m(self):\n        helper()\n"
    pf = _pf(content)
    assert extract_symbols(pf) == extract_file(pf).symbols


@pytest.mark.unit
def test_extraction_is_deterministic() -> None:
    content = "import a.b\nclass C:\n    def m(self):\n        helper()\n        other.call()\n"
    pf = _pf(content)
    first = extract_file(pf).edges
    for _ in range(5):
        assert extract_file(pf).edges == first
