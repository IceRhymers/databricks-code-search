"""Single source of truth for language detection, symbol node-types, and data carriers.

Both :mod:`indexer.parse` (extension -> language) and :mod:`indexer.symbols`
(language -> tree-sitter node-type -> symbol kind) import from here so they can
never disagree on language names. Language values MUST be valid
``tree_sitter_language_pack`` parser names.
"""

from __future__ import annotations

from dataclasses import dataclass

# Largest file (in bytes) we will read and store. Files above this are skipped by
# the walker; a previously-indexed file that grows past the cap is dropped on the
# next mark-and-sweep run.
MAX_FILE_BYTES = 1_000_000

# Lowercase file suffix (including the dot) -> tree_sitter_language_pack language.
EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
}

# Per language: tree-sitter node ``.type`` -> symbol ``kind`` stored in ``symbols``.
# Every top-level key MUST be a value in EXT_TO_LANG (enforced by a unit test).
SYMBOL_KINDS: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
    },
    "javascript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
    },
    "tsx": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_spec": "type",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "method_declaration": "method",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
    },
}


@dataclass(frozen=True)
class ParsedFile:
    """A text file selected for indexing. ``lang`` is None for unknown extensions."""

    path: str
    lang: str | None
    size: int
    content: str


@dataclass(frozen=True)
class ExtractedSymbol:
    """A named symbol extracted from a source file (1-based line numbers)."""

    name: str
    kind: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class IndexCounts:
    """Row-count summary returned by ``index_repo`` for one repo's run."""

    files: int
    symbols: int
    swept: int
