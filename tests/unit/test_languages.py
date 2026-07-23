"""Unit tests for the language/symbol source-of-truth maps."""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint
from tree_sitter_language_pack import get_parser

from app.db.models import ReferenceEdge
from indexer.languages import EDGE_NODE_KINDS, EXT_TO_LANG, MAX_FILE_BYTES, SYMBOL_KINDS
from indexer.symbols import _EDGE_EXTRACTORS


@pytest.mark.unit
def test_symbol_kind_languages_have_no_orphans() -> None:
    """Every language in SYMBOL_KINDS must be a value in EXT_TO_LANG."""
    ext_langs = set(EXT_TO_LANG.values())
    assert set(SYMBOL_KINDS) <= ext_langs, "SYMBOL_KINDS has a language with no extension mapping"


@pytest.mark.unit
def test_edge_node_kind_languages_have_no_orphans() -> None:
    """Every language in EDGE_NODE_KINDS must be a value in EXT_TO_LANG."""
    ext_langs = set(EXT_TO_LANG.values())
    assert set(EDGE_NODE_KINDS) <= ext_langs, (
        "EDGE_NODE_KINDS has a language with no extension mapping"
    )


@pytest.mark.unit
def test_edge_node_kinds_are_within_the_db_check_set() -> None:
    """Every EDGE_NODE_KINDS value must satisfy reference_edges' edge_kind CHECK constraint.

    Cross-checked against the constraint's actual SQL text -- the
    languages-map<->schema tripwire -- rather than a hardcoded duplicate set, so
    a schema change that narrows/renames the allowed kinds is caught here too.
    """
    check = next(
        c
        for c in ReferenceEdge.__table__.constraints
        if isinstance(c, CheckConstraint) and c.name == "ck_reference_edges_edge_kind"
    )
    sql = str(check.sqltext)
    allowed = {kind.strip().strip("'") for kind in sql.split("IN")[1].strip(" ()").split(",")}

    mapped_kinds = {kind for kinds in EDGE_NODE_KINDS.values() for kind in kinds.values()}
    assert mapped_kinds <= allowed, f"EDGE_NODE_KINDS has a kind outside the DB CHECK set: {sql!r}"


@pytest.mark.unit
def test_every_symbol_language_has_an_edge_map() -> None:
    """Coverage guard (issue #85): any language that extracts symbols must also
    declare an edge node-map, so a newly-added language can't silently ship
    symbols with no reference edges."""
    missing = set(SYMBOL_KINDS) - set(EDGE_NODE_KINDS)
    assert not missing, (
        f"languages in SYMBOL_KINDS with no EDGE_NODE_KINDS entry: {sorted(missing)}"
    )


@pytest.mark.unit
def test_symbol_and_edge_node_types_are_disjoint_per_language() -> None:
    """_combined_kinds merges the two maps with dict.update, which silently
    clobbers a symbol entry on collision; the merge's losslessness is enforced
    here, not assumed."""
    for lang, kinds in SYMBOL_KINDS.items():
        overlap = set(kinds) & set(EDGE_NODE_KINDS.get(lang, {}))
        assert not overlap, (
            f"{lang}: node types in both SYMBOL_KINDS and EDGE_NODE_KINDS: {sorted(overlap)}"
        )


@pytest.mark.unit
def test_every_symbol_language_has_an_edge_extractor() -> None:
    """extract_file indexes _EDGE_EXTRACTORS[lang] unconditionally for any
    parsed language; a missing entry is a runtime KeyError for every file of
    that language, so this guard is mandatory."""
    missing = set(SYMBOL_KINDS) - set(_EDGE_EXTRACTORS)
    assert not missing, f"languages with symbols but no edge extractor: {sorted(missing)}"


@pytest.mark.unit
@pytest.mark.parametrize("lang", sorted(set(EXT_TO_LANG.values())))
def test_get_parser_succeeds_for_each_language(lang: str) -> None:
    assert get_parser(lang) is not None


@pytest.mark.unit
def test_ext_keys_are_lowercase_with_dot() -> None:
    for ext in EXT_TO_LANG:
        assert ext.startswith(".") and ext == ext.lower()


@pytest.mark.unit
def test_max_file_bytes_positive() -> None:
    assert MAX_FILE_BYTES > 0
