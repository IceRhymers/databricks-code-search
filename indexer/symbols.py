"""Extract named symbols from a source file using tree-sitter.

Node-type -> symbol kind comes from :data:`indexer.languages.SYMBOL_KINDS`.
Parsers are fetched once per language and cached (tree-sitter parser construction
is relatively expensive; the indexer processes many files of the same language).

The cache is **per-thread**, as insurance against an upstream change, not because
a shared parser is known to corrupt trees. py-tree-sitter 0.26.0 does not release
the GIL inside ``parse()``, and a shared parser measured clean under an 8-thread
torture test — but the GIL-release primitives are linked into that C extension,
so a future release wrapping ``parse()`` in ``Py_BEGIN_ALLOW_THREADS`` would make
this a silent data race with no signal at the call site. See
``.omc/plans/indexing-parallelism.md`` (Step 0) for the measurements.
"""

from __future__ import annotations

import threading
from typing import Any

from tree_sitter_language_pack import get_parser

from indexer.languages import (
    EDGE_NODE_KINDS,
    SYMBOL_KINDS,
    ExtractedEdge,
    ExtractedSymbol,
    FileExtraction,
    ParsedFile,
)

_PARSER_CACHE = threading.local()

# Per-language SYMBOL_KINDS + EDGE_NODE_KINDS merged into one ``node.type -> (tag,
# value)`` map, built once per language and cached here. The two source maps never
# share a node type (a definition node is never also a call/import node), so this
# is a lossless merge -- and it turns the hot walk's two dict lookups per node
# (one symbol-map miss, one edge-map miss, for every ordinary node) into one.
_COMBINED_CACHE: dict[str, dict[str, tuple[str, str]]] = {}


def _combined_kinds(lang: str) -> dict[str, tuple[str, str]] | None:
    combined = _COMBINED_CACHE.get(lang)
    if combined is not None:
        return combined
    kind_map = SYMBOL_KINDS.get(lang)
    if kind_map is None:
        return None
    combined = {node_type: ("symbol", kind) for node_type, kind in kind_map.items()}
    combined.update(
        (node_type, ("edge", edge_kind))
        for node_type, edge_kind in EDGE_NODE_KINDS.get(lang, {}).items()
    )
    _COMBINED_CACHE[lang] = combined
    return combined


def _parser_for(lang: str) -> Any:
    cache: dict[str, Any] | None = getattr(_PARSER_CACHE, "parsers", None)
    if cache is None:
        cache = _PARSER_CACHE.parsers = {}
    parser = cache.get(lang)
    if parser is None:
        parser = cache[lang] = get_parser(lang)
    return parser


def extract_file(pf: ParsedFile) -> FileExtraction:
    """Return the named symbols and reference edges in ``pf``, from one parse and one walk.

    Walks the whole parse tree (so nested definitions -- a method inside a class --
    are captured) exactly once, emitting both symbols and edges as it goes; a file
    whose language has no kind map short-circuits before parsing. Anonymous
    definition nodes (no ``name`` field) are skipped for symbols but stay
    transparent for edge attribution: their children inherit the enclosing symbol
    they would otherwise have replaced. Edges attribute to the innermost NAMED
    enclosing definition on the stack at the time the call/import node is visited;
    ``None`` means module/top-level scope. Line numbers are 1-based.

    Two parallel stacks (node, enclosing-symbol) rather than one stack of pairs --
    pushing a same-enclosing child run via ``[enclosing] * len(children)`` is a
    single C-level list replication instead of N per-child tuple allocations,
    measurably cheaper for the common case (most nodes don't change the enclosing).
    """
    lang = pf.lang
    if lang is None:
        return FileExtraction(symbols=[], edges=[])
    combined = _combined_kinds(lang)
    if combined is None:
        return FileExtraction(symbols=[], edges=[])

    tree = _parser_for(lang).parse(pf.content.encode("utf-8"))
    symbols: list[ExtractedSymbol] = []
    edges: list[ExtractedEdge] = []

    node_stack: list[Any] = [tree.root_node]
    enclosing_stack: list[ExtractedSymbol | None] = [None]
    while node_stack:
        node = node_stack.pop()
        enclosing = enclosing_stack.pop()
        child_enclosing = enclosing

        tag_kind = combined.get(node.type)
        if tag_kind is not None:
            tag, kind = tag_kind
            if tag == "symbol":
                name_node = node.child_by_field_name("name")
                if name_node is not None and name_node.text is not None:
                    symbol = ExtractedSymbol(
                        name=name_node.text.decode("utf-8"),
                        kind=kind,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                    symbols.append(symbol)
                    child_enclosing = symbol
            elif kind == "call":
                edge = _python_call_edge(node, enclosing)
                if edge is not None:
                    edges.append(edge)
            else:  # kind == "import"
                edges.extend(_python_import_edges(node, enclosing))

        children = node.children
        if children:
            node_stack.extend(reversed(children))
            enclosing_stack.extend([child_enclosing] * len(children))

    return FileExtraction(symbols=symbols, edges=edges)


def extract_symbols(pf: ParsedFile) -> list[ExtractedSymbol]:
    """Return the named symbols in ``pf``; ``[]`` for files with no kind map.

    Thin wrapper over :func:`extract_file` kept for the existing unit-test
    surface and any external callers that only need symbols.
    """
    return extract_file(pf).symbols


def _python_call_target(node: Any) -> str | None:
    """Rightmost identifier of a ``call`` node's callee, or ``None`` for candidates with none.

    ``f(...)`` -> ``f``; ``a.b.f(...)``/``self.f(...)`` -> ``f`` (the grammar's
    ``attribute`` field on an ``attribute`` node is always the rightmost
    identifier, so no manual recursion is needed). Callees with no rightmost
    identifier -- ``xs[0]()``, the outer call of ``f()()`` -- are skipped.
    """
    func = node.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "identifier":
        return func.text.decode("utf-8") if func.text is not None else None
    if func.type == "attribute":
        attr = func.child_by_field_name("attribute")
        if attr is not None and attr.text is not None:
            return attr.text.decode("utf-8")
        return None
    return None


def _python_call_edge(node: Any, enclosing: ExtractedSymbol | None) -> ExtractedEdge | None:
    target = _python_call_target(node)
    if target is None:
        return None
    return ExtractedEdge(
        kind="call", target=target, line=node.start_point[0] + 1, enclosing=enclosing
    )


def _python_import_name(node: Any) -> tuple[str | None, Any]:
    """Dotted-path text and line-anchor node for one ``name``-field child of an import.

    ``dotted_name`` -> its own text (``import a.b.c`` -> ``a.b.c``). ``aliased_import``
    -> its inner ``name`` field's text, ignoring the alias (``import a.b.c as d`` ->
    ``a.b.c``; the alias is a local binding, not the target).
    """
    if node.type == "aliased_import":
        inner = node.child_by_field_name("name")
        if inner is not None and inner.text is not None:
            return inner.text.decode("utf-8"), node
        return None, node
    if node.text is not None:
        return node.text.decode("utf-8"), node
    return None, node


def _python_join_module(module_prefix: str, name: str) -> str:
    """Join a ``from``-import's module path to one imported name (D5's join rule).

    Pure-dots relative modules (module text ending in ``.``, e.g. ``from . import x``
    -> ``.``) concatenate directly (-> ``.x``); anything else (``a.b``, ``..p``) joins
    with a literal dot (-> ``a.b.c``, ``..p.q``).
    """
    if not module_prefix:
        return name
    if module_prefix.endswith("."):
        return f"{module_prefix}{name}"
    return f"{module_prefix}.{name}"


def _python_import_edges(node: Any, enclosing: ExtractedSymbol | None) -> list[ExtractedEdge]:
    """Edges for one ``import_statement``/``import_from_statement`` node (D5).

    ``import a.b.c, d`` -> one edge per ``name``-field child, each the full dotted
    path as written (alias-insensitive). ``from a.b import c, d as e`` -> the module
    path joined to each *original* imported name. ``from a.b import *`` -> one edge
    for the module path itself, anchored at the ``wildcard_import`` node's line.
    Per-name edges take the name node's own start line (correct for multi-line
    parenthesized imports).
    """
    edges: list[ExtractedEdge] = []
    if node.type == "import_statement":
        for name_node in node.children_by_field_name("name"):
            target, anchor = _python_import_name(name_node)
            if target is not None:
                edges.append(
                    ExtractedEdge(
                        kind="import",
                        target=target,
                        line=anchor.start_point[0] + 1,
                        enclosing=enclosing,
                    )
                )
        return edges

    # import_from_statement
    module_node = node.child_by_field_name("module_name")
    module_prefix = (
        module_node.text.decode("utf-8")
        if module_node is not None and module_node.text is not None
        else ""
    )
    name_nodes = node.children_by_field_name("name")
    if name_nodes:
        for name_node in name_nodes:
            bare, anchor = _python_import_name(name_node)
            if bare is not None:
                edges.append(
                    ExtractedEdge(
                        kind="import",
                        target=_python_join_module(module_prefix, bare),
                        line=anchor.start_point[0] + 1,
                        enclosing=enclosing,
                    )
                )
        return edges

    if module_prefix:
        wildcard = next((c for c in node.children if c.type == "wildcard_import"), None)
        if wildcard is not None:
            edges.append(
                ExtractedEdge(
                    kind="import",
                    target=module_prefix,
                    line=wildcard.start_point[0] + 1,
                    enclosing=enclosing,
                )
            )
    return edges
