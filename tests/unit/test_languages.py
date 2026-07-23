"""Unit tests for the language/symbol source-of-truth maps."""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint
from tree_sitter_language_pack import get_parser

from app.db.models import ReferenceEdge
from indexer.languages import EDGE_NODE_KINDS, EXT_TO_LANG, MAX_FILE_BYTES, SYMBOL_KINDS


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
