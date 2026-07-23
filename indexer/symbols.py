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
from collections.abc import Callable
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

    Call/import extraction is dispatched per-language via ``_EDGE_EXTRACTORS``,
    resolved once per file (not per node) immediately after the combined map is
    found, since that lookup already guarantees ``lang in SYMBOL_KINDS``.
    """
    lang = pf.lang
    if lang is None:
        return FileExtraction(symbols=[], edges=[])
    combined = _combined_kinds(lang)
    if combined is None:
        return FileExtraction(symbols=[], edges=[])
    call_edge, import_edges = _EDGE_EXTRACTORS[lang]

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
                edge = call_edge(node, enclosing)
                if edge is not None:
                    edges.append(edge)
            else:  # kind == "import"
                edges.extend(import_edges(node, enclosing))

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


def _js_string_fragment_text(string_node: Any) -> str:
    """Unquoted text of a JS/TS ``string`` node (its ``string_fragment`` child)."""
    frag = next((c for c in string_node.children if c.type == "string_fragment"), None)
    return frag.text.decode("utf-8") if frag is not None and frag.text is not None else ""


def _js_call_edge(node: Any, enclosing: ExtractedSymbol | None) -> ExtractedEdge | None:
    """Rightmost-name target for a JS/TS/TSX ``call_expression``/``new_expression`` (#85).

    Callee field is ``function`` for calls, ``constructor`` for ``new``. A bare
    ``identifier`` callee is its own target (``f()``, ``require(...)``); a
    ``member_expression`` callee targets its ``property`` field, unaffected by an
    optional chain (``a.b.f()``/``obj?.m()`` -> ``f``/``m``; ``new a.b.Foo()`` ->
    ``Foo``). Any other callee shape -- subscript, an outer call-of-call, or the
    ``import`` keyword node of a dynamic ``import(...)`` -- is skipped.
    """
    field = "constructor" if node.type == "new_expression" else "function"
    func = node.child_by_field_name(field)
    if func is None:
        return None
    target: str | None = None
    if func.type == "identifier":
        target = func.text.decode("utf-8") if func.text is not None else None
    elif func.type == "member_expression":
        prop = func.child_by_field_name("property")
        if prop is not None and prop.text is not None:
            target = prop.text.decode("utf-8")
    if target is None:
        return None
    return ExtractedEdge(
        kind="call", target=target, line=node.start_point[0] + 1, enclosing=enclosing
    )


def _js_import_edges(node: Any, enclosing: ExtractedSymbol | None) -> list[ExtractedEdge]:
    """Edges for one JS/TS/TSX ``import_statement`` (#85), per the A8 anchoring rule.

    TS ``import x = require('legacy')`` is handled first via its
    ``import_require_clause`` child (its own ``source`` field), anchored at the
    statement line. Otherwise the statement's own ``source`` field gives the
    module string; an empty source or missing ``import_clause`` (side-effect
    import) yields a single statement-anchored edge for the bare module (or none,
    for an empty source). Within a clause: a bare ``identifier`` (default import)
    or a ``namespace_import`` each yield one statement-anchored edge targeting the
    module; each ``import_specifier`` in a ``named_imports`` block yields one
    specifier-anchored edge (alias ignored -- D5) targeting ``module.name``.
    """
    stmt_line = node.start_point[0] + 1
    require_clause = next((c for c in node.children if c.type == "import_require_clause"), None)
    if require_clause is not None:
        req_source = require_clause.child_by_field_name("source")
        if req_source is None:
            return []
        target = _js_string_fragment_text(req_source)
        if not target:
            return []
        return [ExtractedEdge(kind="import", target=target, line=stmt_line, enclosing=enclosing)]

    source_node = node.child_by_field_name("source")
    if source_node is None:
        return []
    source = _js_string_fragment_text(source_node)
    if not source:
        return []

    clause = next((c for c in node.children if c.type == "import_clause"), None)
    if clause is None:
        return [ExtractedEdge(kind="import", target=source, line=stmt_line, enclosing=enclosing)]

    edges: list[ExtractedEdge] = []
    for child in clause.children:
        if child.type in ("identifier", "namespace_import"):
            edges.append(
                ExtractedEdge(kind="import", target=source, line=stmt_line, enclosing=enclosing)
            )
        elif child.type == "named_imports":
            for spec in child.children:
                if spec.type != "import_specifier":
                    continue
                name_node = spec.child_by_field_name("name")
                if name_node is None or name_node.text is None:
                    continue
                edges.append(
                    ExtractedEdge(
                        kind="import",
                        target=f"{source}.{name_node.text.decode('utf-8')}",
                        line=spec.start_point[0] + 1,
                        enclosing=enclosing,
                    )
                )
    return edges


def _go_call_edge(node: Any, enclosing: ExtractedSymbol | None) -> ExtractedEdge | None:
    """Rightmost-name target for a Go ``call_expression`` (#85).

    ``function`` field ``identifier`` -> its own text (``f()``); ``selector_expression``
    -> its ``field`` field text (``pkg.F()`` -> ``F``, ``obj.Method()`` -> ``Method``).
    ``go``/``defer`` wrap an ordinary inner ``call_expression``, so they need no
    special-casing here -- the walk visits the inner node directly.
    """
    func = node.child_by_field_name("function")
    if func is None:
        return None
    target: str | None = None
    if func.type == "identifier":
        target = func.text.decode("utf-8") if func.text is not None else None
    elif func.type == "selector_expression":
        field = func.child_by_field_name("field")
        if field is not None and field.text is not None:
            target = field.text.decode("utf-8")
    if target is None:
        return None
    return ExtractedEdge(
        kind="call", target=target, line=node.start_point[0] + 1, enclosing=enclosing
    )


def _go_import_edges(node: Any, enclosing: ExtractedSymbol | None) -> list[ExtractedEdge]:
    """One edge per Go ``import_spec`` (#85), mapped instead of ``import_declaration``.

    Per-spec node -> per-import anchors for both single and grouped
    (``import ( ... )``) forms; an empty group yields zero ``import_spec`` nodes and
    needs no special-casing. Target is the *interior* text of the ``path`` field's
    string-literal-content child (A7, binding) -- not ``node.text``, which includes
    the quotes/backticks. The optional ``name`` field (alias, ``.``, ``_``) is
    ignored (D5): dot/blank imports still target the package path.
    """
    path_node = node.child_by_field_name("path")
    if path_node is None:
        return []
    content = next(
        (
            c
            for c in path_node.children
            if c.type in ("interpreted_string_literal_content", "raw_string_literal_content")
        ),
        None,
    )
    if content is not None and content.text is not None:
        target = content.text.decode("utf-8")
    elif path_node.text is not None:
        target = path_node.text.decode("utf-8").strip('"`')
    else:
        target = ""
    if not target:
        return []
    return [
        ExtractedEdge(
            kind="import", target=target, line=node.start_point[0] + 1, enclosing=enclosing
        )
    ]


def _java_type_name(type_node: Any) -> str | None:
    """Rightmost simple type name for a Java ``object_creation_expression`` target (A1).

    A ``generic_type`` first descends to its underlying type node (its first named
    child, dropping ``type_arguments``). A ``type_identifier`` is its own text
    (``Foo``). A ``scoped_type_identifier`` has no ``name`` field -- the grammar
    exposes its segments as *unnamed* ``type_identifier`` children -- so the target
    is the text of the **last** such child (``a.b.Foo`` -> ``Foo``). Any other shape
    is skipped.
    """
    if type_node.type == "generic_type":
        inner = type_node.named_children[0] if type_node.named_children else None
        if inner is None:
            return None
        type_node = inner
    if type_node.type == "type_identifier":
        return type_node.text.decode("utf-8") if type_node.text is not None else None
    if type_node.type == "scoped_type_identifier":
        last = None
        for child in type_node.children:
            if child.type == "type_identifier":
                last = child
        return last.text.decode("utf-8") if last is not None and last.text is not None else None
    return None


def _java_call_edge(node: Any, enclosing: ExtractedSymbol | None) -> ExtractedEdge | None:
    """Target for a Java ``method_invocation``/``object_creation_expression`` (#85).

    ``method_invocation`` -> its ``name`` field text, regardless of the optional
    ``object`` field or generic ``type_arguments`` (``obj.<T>m()`` -> ``m``).
    ``object_creation_expression`` -> :func:`_java_type_name` of its ``type`` field
    (A1).
    """
    if node.type == "method_invocation":
        name = node.child_by_field_name("name")
        if name is None or name.text is None:
            return None
        target: str | None = name.text.decode("utf-8")
    else:  # object_creation_expression
        type_node = node.child_by_field_name("type")
        target = _java_type_name(type_node) if type_node is not None else None
    if target is None:
        return None
    return ExtractedEdge(
        kind="call", target=target, line=node.start_point[0] + 1, enclosing=enclosing
    )


def _java_import_edges(node: Any, enclosing: ExtractedSymbol | None) -> list[ExtractedEdge]:
    """One edge per Java ``import_declaration`` (#85) -- no grouping in this grammar.

    Target is the text of the statement's ``scoped_identifier`` (or bare
    ``identifier``) child, as written: plain (``a.b.C``), static (``a.b.C.m`` --
    the full text already includes the member), and wildcard (``a.b`` -- the
    package; the sibling ``asterisk`` node carries no field and is ignored).
    """
    ident = next((c for c in node.children if c.type in ("scoped_identifier", "identifier")), None)
    if ident is None or ident.text is None:
        return []
    return [
        ExtractedEdge(
            kind="import",
            target=ident.text.decode("utf-8"),
            line=node.start_point[0] + 1,
            enclosing=enclosing,
        )
    ]


def _rust_call_edge(node: Any, enclosing: ExtractedSymbol | None) -> ExtractedEdge | None:
    """Rightmost-name target for a Rust ``call_expression`` (#85).

    ``function`` field ``identifier`` -> its own text (``f()``); ``scoped_identifier``
    -> its ``name`` field, rightmost (``a::b::g()`` -> ``g``, ``Foo::new()`` -> ``new``);
    ``field_expression`` -> its ``field`` field (``x.method()`` -> ``method``). Any
    other callee -- notably ``macro_invocation`` (``println!(...)``), which is
    unmapped and so never even reaches here -- is skipped.
    """
    func = node.child_by_field_name("function")
    if func is None:
        return None
    target: str | None = None
    if func.type == "identifier":
        target = func.text.decode("utf-8") if func.text is not None else None
    elif func.type == "scoped_identifier":
        name = func.child_by_field_name("name")
        if name is not None and name.text is not None:
            target = name.text.decode("utf-8")
    elif func.type == "field_expression":
        field = func.child_by_field_name("field")
        if field is not None and field.text is not None:
            target = field.text.decode("utf-8")
    if target is None:
        return None
    return ExtractedEdge(
        kind="call", target=target, line=node.start_point[0] + 1, enclosing=enclosing
    )


def _rust_join_path(prefix: str, segment: str) -> str:
    """Join a Rust use-tree ``prefix`` accumulator to one more path ``segment``."""
    return segment if not prefix else f"{prefix}::{segment}"


def _rust_use_tree_edges(
    node: Any, prefix: str, enclosing: ExtractedSymbol | None
) -> list[ExtractedEdge]:
    """Recursive use-tree descent for one node of a Rust ``use_declaration`` (A2/A3).

    ``prefix`` is the accumulated path text from enclosing ``scoped_use_list``
    levels (``""`` at the top). A leaf ``identifier``/``scoped_identifier`` emits
    ``join(prefix, its text)``; a ``use_as_clause`` emits ``join(prefix, path-field
    text)`` (alias ignored -- D5); a ``use_wildcard`` emits ``join(prefix, inner
    path text)`` if it has an inner path child, else ``prefix`` unchanged; a bare
    ``self`` node (only reachable as a ``use_list`` item) emits ``prefix``
    unchanged; a ``scoped_use_list`` extends the prefix with its own ``path`` field
    and recurses into each named child of its ``list``. Every edge anchors at its
    own leaf node's start line.
    """
    if node.type == "self":
        return (
            [
                ExtractedEdge(
                    kind="import", target=prefix, line=node.start_point[0] + 1, enclosing=enclosing
                )
            ]
            if prefix
            else []
        )
    if node.type in ("identifier", "scoped_identifier"):
        if node.text is None:
            return []
        return [
            ExtractedEdge(
                kind="import",
                target=_rust_join_path(prefix, node.text.decode("utf-8")),
                line=node.start_point[0] + 1,
                enclosing=enclosing,
            )
        ]
    if node.type == "use_as_clause":
        path_node = node.child_by_field_name("path")
        if path_node is None or path_node.text is None:
            return []
        return [
            ExtractedEdge(
                kind="import",
                target=_rust_join_path(prefix, path_node.text.decode("utf-8")),
                line=node.start_point[0] + 1,
                enclosing=enclosing,
            )
        ]
    if node.type == "use_wildcard":
        inner = next(
            (c for c in node.children if c.type in ("identifier", "scoped_identifier")), None
        )
        target = (
            _rust_join_path(prefix, inner.text.decode("utf-8"))
            if inner is not None and inner.text is not None
            else prefix
        )
        if not target:
            return []
        return [
            ExtractedEdge(
                kind="import", target=target, line=node.start_point[0] + 1, enclosing=enclosing
            )
        ]
    if node.type == "scoped_use_list":
        path_node = node.child_by_field_name("path")
        new_prefix = (
            _rust_join_path(prefix, path_node.text.decode("utf-8"))
            if path_node is not None and path_node.text is not None
            else prefix
        )
        list_node = node.child_by_field_name("list")
        edges: list[ExtractedEdge] = []
        if list_node is not None:
            for child in list_node.named_children:
                edges.extend(_rust_use_tree_edges(child, new_prefix, enclosing))
        return edges
    if node.type == "use_list":
        # A prefix-less group -- ``use {std::io, std::fmt};`` -- is a bare
        # ``use_list`` with no enclosing ``scoped_use_list``; each item keeps the
        # current (possibly empty) prefix unchanged.
        edges = []
        for child in node.named_children:
            edges.extend(_rust_use_tree_edges(child, prefix, enclosing))
        return edges
    return []


def _rust_import_edges(node: Any, enclosing: ExtractedSymbol | None) -> list[ExtractedEdge]:
    """Edges for one Rust ``use_declaration`` (#85): recurse its ``argument`` use-tree."""
    argument = node.child_by_field_name("argument")
    if argument is None:
        return []
    return _rust_use_tree_edges(argument, "", enclosing)


CallEdgeFn = Callable[[Any, "ExtractedSymbol | None"], "ExtractedEdge | None"]
ImportEdgesFn = Callable[[Any, "ExtractedSymbol | None"], list["ExtractedEdge"]]

_EDGE_EXTRACTORS: dict[str, tuple[CallEdgeFn, ImportEdgesFn]] = {
    "python": (_python_call_edge, _python_import_edges),
    "javascript": (_js_call_edge, _js_import_edges),
    "typescript": (_js_call_edge, _js_import_edges),
    "tsx": (_js_call_edge, _js_import_edges),
    "go": (_go_call_edge, _go_import_edges),
    "java": (_java_call_edge, _java_import_edges),
    "rust": (_rust_call_edge, _rust_import_edges),
}
