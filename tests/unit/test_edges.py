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


def _edges(content: str, lang: str | None = "python") -> list[ExtractedEdge]:
    return extract_file(_pf(content, lang=lang)).edges


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
def test_none_lang_yields_no_edges() -> None:
    assert extract_file(_pf("f(x)\n", lang=None)).edges == []


@pytest.mark.unit
def test_javascript_call_and_import_edges_on_separate_lines() -> None:
    content = "f(x);\nimport { a } from 'b';\n"
    edges = _edges(content, lang="javascript")
    assert [(e.kind, e.target, e.line) for e in edges] == [
        ("call", "f", 1),
        ("import", "b.a", 2),
    ]


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


@pytest.mark.unit
def test_extraction_is_deterministic_for_a_non_python_language() -> None:
    content = "use a::b::{c, d};\nfn f() { a::b::c(); }\n"
    pf = _pf(content, lang="rust")
    first = extract_file(pf).edges
    for _ in range(5):
        assert extract_file(pf).edges == first


# --- #85: JavaScript / TypeScript / TSX --------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("f(x);", [("call", "f", 1)]),
        ("a.b.f();", [("call", "f", 1)]),
        ("obj?.m();", [("call", "m", 1)]),
        ("new Foo();", [("call", "Foo", 1)]),
        ("new a.b.Foo();", [("call", "Foo", 1)]),
        ("require('y');", [("call", "require", 1)]),
        ("import d from 'm';", [("import", "m", 1)]),
        (
            "import { a, b as c } from 'mod';",
            [("import", "mod.a", 1), ("import", "mod.b", 1)],
        ),
        ("import * as ns from 'ns';", [("import", "ns", 1)]),
        ("import 'side-effect';", [("import", "side-effect", 1)]),
        (
            "import d, { a } from 'm';",
            [("import", "m", 1), ("import", "m.a", 1)],
        ),
        ("export { z } from 'w';", []),
        ("import('m').then(f);", [("call", "then", 1)]),
        ("import('dyn');", []),
    ],
)
def test_javascript_shape_fixtures(content: str, expected: list[tuple[str, str, int]]) -> None:
    edges = _edges(content, lang="javascript")
    assert [(e.kind, e.target, e.line) for e in edges] == expected


@pytest.mark.unit
def test_javascript_multiline_named_import_per_specifier_lines() -> None:
    content = "import {\n  a,\n  b,\n} from 'mod';\n"
    edges = _edges(content, lang="javascript")
    assert [(e.kind, e.target, e.line) for e in edges] == [
        ("import", "mod.a", 2),
        ("import", "mod.b", 3),
    ]


@pytest.mark.unit
def test_typescript_generic_call_and_import_type_and_require() -> None:
    assert [(e.kind, e.target, e.line) for e in _edges("f<T>(x);", lang="typescript")] == [
        ("call", "f", 1)
    ]
    assert [
        (e.kind, e.target, e.line) for e in _edges("import type { T } from 'm';", lang="typescript")
    ] == [("import", "m.T", 1)]
    assert [
        (e.kind, e.target, e.line)
        for e in _edges("import x = require('legacy');", lang="typescript")
    ] == [("import", "legacy", 1)]


@pytest.mark.unit
def test_tsx_jsx_component_ignored_inner_call_captured() -> None:
    content = "const e = <Comp prop={g()} />;\n"
    edges = _edges(content, lang="tsx")
    assert [(e.kind, e.target, e.line) for e in edges] == [("call", "g", 1)]


@pytest.mark.unit
def test_tsx_plain_named_import() -> None:
    content = "import { Comp } from './comp';\n"
    edges = _edges(content, lang="tsx")
    assert [(e.kind, e.target, e.line) for e in edges] == [("import", "./comp.Comp", 1)]


@pytest.mark.unit
def test_javascript_and_typescript_share_the_same_call_and_import_extractors() -> None:
    content = "f(x);\nimport { a } from 'm';\n"
    assert _edges(content, lang="javascript") == _edges(content, lang="typescript")


# --- #85: Go ------------------------------------------------------------------


def _go(body: str) -> str:
    return f"package main\nfunc f() {{\n{body}}}\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "expected_target"),
    [
        ("  f(x)\n", "f"),
        ("  pkg.F()\n", "F"),
        ("  obj.Method()\n", "Method"),
        ("  go h()\n", "h"),
        ("  defer cleanup()\n", "cleanup"),
    ],
)
def test_go_call_shape_fixtures(body: str, expected_target: str) -> None:
    edges = _edges(_go(body), lang="go")
    assert [e.target for e in edges if e.kind == "call"] == [expected_target]


@pytest.mark.unit
def test_go_single_imports() -> None:
    assert [(e.kind, e.target) for e in _edges('package main\nimport "fmt"\n', lang="go")] == [
        ("import", "fmt")
    ]
    assert [
        (e.kind, e.target) for e in _edges('package main\nimport "github.com/x/y"\n', lang="go")
    ] == [("import", "github.com/x/y")]


@pytest.mark.unit
def test_go_grouped_import_per_spec_lines_and_aliases_ignored() -> None:
    content = 'package main\nimport (\n  "fmt"\n  m "math"\n  . "strings"\n  _ "driver"\n)\n'
    edges = _edges(content, lang="go")
    assert [(e.kind, e.target, e.line) for e in edges] == [
        ("import", "fmt", 3),
        ("import", "math", 4),
        ("import", "strings", 5),
        ("import", "driver", 6),
    ]


@pytest.mark.unit
def test_go_empty_import_group_yields_no_edges() -> None:
    content = "package main\nimport (\n)\n"
    assert _edges(content, lang="go") == []


# --- #85: Java ------------------------------------------------------------------


def _java(body: str) -> str:
    return f"class C {{\n  void f() {{\n{body}  }}\n}}\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "expected_target"),
    [
        ("    f();\n", "f"),
        ("    obj.m();\n", "m"),
        ("    C.stat();\n", "stat"),
        ("    this.n();\n", "n"),
        ("    super.s();\n", "s"),
        ("    obj.<T>m();\n", "m"),
        ("    new Foo();\n", "Foo"),
        ("    new a.b.Foo();\n", "Foo"),
        ("    new Foo<T>();\n", "Foo"),
        ("    new java.util.ArrayList<String>();\n", "ArrayList"),
    ],
)
def test_java_call_shape_fixtures(body: str, expected_target: str) -> None:
    edges = _edges(_java(body), lang="java")
    assert [e.target for e in edges if e.kind == "call"] == [expected_target]


@pytest.mark.unit
def test_java_import_shapes() -> None:
    assert [(e.kind, e.target) for e in _edges("import a.b.C;", lang="java")] == [
        ("import", "a.b.C")
    ]
    assert [(e.kind, e.target) for e in _edges("import static a.b.C.m;", lang="java")] == [
        ("import", "a.b.C.m")
    ]
    assert [(e.kind, e.target) for e in _edges("import a.b.*;", lang="java")] == [("import", "a.b")]


# --- #85: Rust ------------------------------------------------------------------


def _rust(body: str) -> str:
    return f"fn f() {{\n{body}}}\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "expected_target"),
    [
        ("  f();\n", "f"),
        ("  a::b::g();\n", "g"),
        ("  Foo::new();\n", "new"),
        ("  x.method();\n", "method"),
    ],
)
def test_rust_call_shape_fixtures(body: str, expected_target: str) -> None:
    edges = _edges(_rust(body), lang="rust")
    assert [e.target for e in edges if e.kind == "call"] == [expected_target]


@pytest.mark.unit
def test_rust_macro_invocation_is_not_an_edge() -> None:
    edges = _edges(_rust('  println!("hi");\n'), lang="rust")
    assert edges == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("use a::b::c;", [("import", "a::b::c")]),
        ("use g as h;", [("import", "g")]),
        ("use a::*;", [("import", "a")]),
        ("use crate::x;", [("import", "crate::x")]),
        ("use super::y;", [("import", "super::y")]),
        ("use self::z;", [("import", "self::z")]),
    ],
)
def test_rust_use_shape_fixtures(content: str, expected: list[tuple[str, str]]) -> None:
    edges = _edges(content, lang="rust")
    assert [(e.kind, e.target) for e in edges] == expected


@pytest.mark.unit
def test_rust_use_grouped_renamed_per_item() -> None:
    edges = _edges("use a::b::{d, e as f};", lang="rust")
    assert [(e.kind, e.target) for e in edges] == [
        ("import", "a::b::d"),
        ("import", "a::b::e"),
    ]


@pytest.mark.unit
def test_rust_use_self_in_group_and_sibling() -> None:
    edges = _edges("use a::b::{self, d};", lang="rust")
    assert [(e.kind, e.target) for e in edges] == [
        ("import", "a::b"),
        ("import", "a::b::d"),
    ]


@pytest.mark.unit
def test_rust_use_nested_scoped_lists() -> None:
    edges = _edges("use a::{b::{c, d}};", lang="rust")
    assert [(e.kind, e.target) for e in edges] == [
        ("import", "a::b::c"),
        ("import", "a::b::d"),
    ]


@pytest.mark.unit
def test_rust_use_bare_prefix_less_group() -> None:
    """``use {a::b, c::d};`` (no leading path) is a bare ``use_list`` argument --
    distinct from ``scoped_use_list``, which always has a ``path`` field."""
    edges = _edges("use {a::b, c::d};", lang="rust")
    assert [(e.kind, e.target) for e in edges] == [
        ("import", "a::b"),
        ("import", "c::d"),
    ]


@pytest.mark.unit
def test_rust_import_attributes_to_enclosing_function() -> None:
    content = "fn outer() {\n  use a::b;\n  c();\n}\n"
    fx = extract_file(_pf(content, lang="rust"))
    import_edge = next(e for e in fx.edges if e.kind == "import")
    assert import_edge.target == "a::b"
    assert import_edge.enclosing is not None
    assert import_edge.enclosing.name == "outer"


@pytest.mark.unit
def test_javascript_default_and_namespace_import_both_target_the_module() -> None:
    """Documents current behavior: a default+namespace combo binds two local
    names to the same module, so both clauses independently emit a
    statement-anchored edge targeting that module (duplicate kind/target/line is
    expected here, not a bug -- there is no name-class target to disambiguate
    module-class default/namespace imports)."""
    edges = _edges("import d, * as ns from 'm';", lang="javascript")
    assert [(e.kind, e.target, e.line) for e in edges] == [
        ("import", "m", 1),
        ("import", "m", 1),
    ]


# --- #85: enclosing attribution per language -----------------------------------


@pytest.mark.unit
def test_javascript_enclosing_attribution_function_method_and_module() -> None:
    content = (
        "function top() {\n"
        "  callInFn();\n"
        "}\n"
        "class C {\n"
        "  m() {\n"
        "    callInMethod();\n"
        "  }\n"
        "}\n"
        "callAtModuleScope();\n"
    )
    fx = extract_file(_pf(content, lang="javascript"))
    by_target = {e.target: e for e in fx.edges}

    fn_edge = by_target["callInFn"]
    assert fn_edge.enclosing is not None
    assert fn_edge.enclosing.name == "top"
    assert fn_edge.enclosing.kind == "function"

    method_edge = by_target["callInMethod"]
    assert method_edge.enclosing is not None
    assert method_edge.enclosing.name == "m"
    assert method_edge.enclosing.kind == "method"

    assert by_target["callAtModuleScope"].enclosing is None


@pytest.mark.unit
def test_go_enclosing_attribution_function_and_method() -> None:
    content = (
        "package main\nfunc top() {\n  callInFn()\n}\nfunc (r *R) m() {\n  callInMethod()\n}\n"
    )
    fx = extract_file(_pf(content, lang="go"))
    by_target = {e.target: e for e in fx.edges}

    fn_edge = by_target["callInFn"]
    assert fn_edge.enclosing is not None
    assert fn_edge.enclosing.name == "top"
    assert fn_edge.enclosing.kind == "function"

    method_edge = by_target["callInMethod"]
    assert method_edge.enclosing is not None
    assert method_edge.enclosing.name == "m"
    assert method_edge.enclosing.kind == "method"


@pytest.mark.unit
def test_java_enclosing_attribution_method_inside_class() -> None:
    content = "class C {\n  void m() {\n    callInMethod();\n  }\n}\n"
    fx = extract_file(_pf(content, lang="java"))
    edge = fx.edges[0]
    assert edge.target == "callInMethod"
    assert edge.enclosing is not None
    assert edge.enclosing.name == "m"
    assert edge.enclosing.kind == "method"


@pytest.mark.unit
def test_java_enclosing_attribution_interface_default_method() -> None:
    content = "interface I {\n  default void m() {\n    callInMethod();\n  }\n}\n"
    fx = extract_file(_pf(content, lang="java"))
    edge = fx.edges[0]
    assert edge.target == "callInMethod"
    assert edge.enclosing is not None
    assert edge.enclosing.name == "m"
    assert edge.enclosing.kind == "method"


@pytest.mark.unit
def test_rust_enclosing_attribution_function_and_impl_method() -> None:
    content = (
        "fn top() {\n"
        "  callInFn();\n"
        "}\n"
        "struct S;\n"
        "impl S {\n"
        "  fn m(&self) {\n"
        "    callInMethod();\n"
        "  }\n"
        "}\n"
        "const X: i32 = { callAtModuleScope(); 1 };\n"
    )
    fx = extract_file(_pf(content, lang="rust"))
    by_target = {e.target: e for e in fx.edges}

    fn_edge = by_target["callInFn"]
    assert fn_edge.enclosing is not None
    assert fn_edge.enclosing.name == "top"
    assert fn_edge.enclosing.kind == "function"

    method_edge = by_target["callInMethod"]
    assert method_edge.enclosing is not None
    assert method_edge.enclosing.name == "m"
    assert method_edge.enclosing.kind == "function"

    assert by_target["callAtModuleScope"].enclosing is None
