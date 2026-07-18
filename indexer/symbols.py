"""Extract named symbols from a source file using tree-sitter.

Node-type -> symbol kind comes from :data:`indexer.languages.SYMBOL_KINDS`.
Parsers are fetched once per language and cached (tree-sitter parser construction
is relatively expensive; the indexer processes many files of the same language).
"""

from __future__ import annotations

from typing import Any

from tree_sitter_language_pack import get_parser

from indexer.languages import SYMBOL_KINDS, ExtractedSymbol, ParsedFile

_PARSER_CACHE: dict[str, Any] = {}


def _parser_for(lang: str) -> Any:
    parser = _PARSER_CACHE.get(lang)
    if parser is None:
        parser = get_parser(lang)
        _PARSER_CACHE[lang] = parser
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
