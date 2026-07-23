"""Single source of truth for language detection, symbol node-types, and data carriers.

Both :mod:`indexer.parse` (extension -> language) and :mod:`indexer.symbols`
(language -> tree-sitter node-type -> symbol kind) import from here so they can
never disagree on language names. Language values MUST be valid
``tree_sitter_language_pack`` parser names. ``EDGE_NODE_KINDS`` maps, per
language, tree-sitter node ``.type`` -> reference-edge kind (``call``/``import``);
a language absent from the map yields zero edges (Python only for #84; #85
adds the rest).
"""

from __future__ import annotations

from dataclasses import dataclass

# Largest file (in bytes) we will read and store. Files above this are skipped by
# the walker; a previously-indexed file that grows past the cap is dropped on the
# next mark-and-sweep run.
MAX_FILE_BYTES = 1_000_000

# Char budget for one embedding chunk (issue #14). V1 uses a char-based
# approximation rather than a real tokenizer: ~4 characters per token, so this
# maps to the default `semantic_chunk_max_tokens` (512) in app.config. Document
# this in one place; `indexer.parse.iter_chunks` is the sole consumer.
SEMANTIC_CHUNK_MAX_CHARS = 2000

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

# Per language: tree-sitter node ``.type`` -> reference-edge kind stored in
# ``reference_edges``. Every value MUST be within the DB CHECK set
# (``ReferenceEdge.__table__``'s ``ck_reference_edges_edge_kind``), enforced by
# a unit test. Python only for #84 -- #85 adds the other six languages.
EDGE_NODE_KINDS: dict[str, dict[str, str]] = {
    "python": {
        "call": "call",
        "import_statement": "import",
        "import_from_statement": "import",
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
class Chunk:
    """One embeddable slice of a file's content (issue #14). 1-based, inclusive lines."""

    chunk_index: int
    content: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ExtractedSymbol:
    """A named symbol extracted from a source file (1-based line numbers)."""

    name: str
    kind: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ExtractedEdge:
    """A raw (unresolved) call/import reference site extracted from a source file.

    ``target`` is a candidate name/dotted-path, not a resolved symbol id (the
    epic #82 rule: resolution happens at query time by name-join). ``enclosing``
    is the innermost NAMED enclosing definition; ``None`` means module scope.
    """

    kind: str  # 'call' | 'import' -- must stay within the reference_edges DB CHECK set
    target: str
    line: int  # 1-based
    enclosing: ExtractedSymbol | None


@dataclass(frozen=True)
class FileExtraction:
    """The one-walk result of parsing a file: its symbols and its reference edges."""

    symbols: list[ExtractedSymbol]
    edges: list[ExtractedEdge]


@dataclass(frozen=True)
class IndexCounts:
    """Row-count summary returned by ``index_repo`` for one repo's run."""

    files: int
    symbols: int
    swept: int
    edges: int
