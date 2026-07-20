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

from indexer.languages import SYMBOL_KINDS, ExtractedSymbol, ParsedFile

_PARSER_CACHE = threading.local()


def _parser_for(lang: str) -> Any:
    cache: dict[str, Any] | None = getattr(_PARSER_CACHE, "parsers", None)
    if cache is None:
        cache = _PARSER_CACHE.parsers = {}
    parser = cache.get(lang)
    if parser is None:
        parser = cache[lang] = get_parser(lang)
    return parser


def extract_symbols(pf: ParsedFile) -> list[ExtractedSymbol]:
    """Return the named symbols in ``pf``; ``[]`` for files with no kind map.

    Walks the whole parse tree (so nested definitions — a method inside a class —
    are captured). Anonymous nodes (no ``name`` field) are skipped. Line numbers
    are 1-based.
    """
    if pf.lang is None:
        return []
    kind_map = SYMBOL_KINDS.get(pf.lang)
    if kind_map is None:
        return []

    tree = _parser_for(pf.lang).parse(pf.content.encode("utf-8"))
    symbols: list[ExtractedSymbol] = []

    cursor_stack = [tree.root_node]
    while cursor_stack:
        node = cursor_stack.pop()
        kind = kind_map.get(node.type)
        if kind is not None:
            name_node = node.child_by_field_name("name")
            if name_node is not None and name_node.text is not None:
                symbols.append(
                    ExtractedSymbol(
                        name=name_node.text.decode("utf-8"),
                        kind=kind,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                    )
                )
        cursor_stack.extend(reversed(node.children))

    return symbols
