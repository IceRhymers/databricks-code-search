"""Unit tests for the language/symbol source-of-truth maps."""

from __future__ import annotations

import pytest
from tree_sitter_language_pack import get_parser

from indexer.languages import EXT_TO_LANG, MAX_FILE_BYTES, SYMBOL_KINDS


@pytest.mark.unit
def test_symbol_kind_languages_have_no_orphans() -> None:
    """Every language in SYMBOL_KINDS must be a value in EXT_TO_LANG."""
    ext_langs = set(EXT_TO_LANG.values())
    assert set(SYMBOL_KINDS) <= ext_langs, "SYMBOL_KINDS has a language with no extension mapping"


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
