"""Cross-language parity gate for issue #47's flat-AND query recognizer.

webui/frontend/src/utils/queryModel.corpus.json is the single source of truth shared
with webui/frontend/src/utils/queryModel.corpus.test.ts: this test asserts the same
entries against the REAL parser (app/query/parser.py), so "safe" in the TS recognizer
can never silently drift from what the backend actually accepts. `verdict` (TS
recognizer's flat-AND classification) and `python_parses` (whether app.query.parser.parse
raises) are deliberately separate axes -- e.g. `""` is TS-safe (a vacuous, zero-atom
query the chip UI is happy to render) but fails to parse in Python (`empty query`), and a
query with real OR/paren/regex/quote structure can be a perfectly valid Python parse while
still being TS-unsafe (the chip UI refuses to rewrite it as flat atoms).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.query.parser import QueryParseError, TokenKind, tokenize

_CORPUS_PATH = (
    Path(__file__).resolve().parents[2] / "webui/frontend/src/utils/queryModel.corpus.json"
)

# Maps the TS Atom.field name (None for a content atom) to the TokenKind tokenize() emits.
_FIELD_TO_KIND: dict[str | None, TokenKind] = {
    "repo": TokenKind.REPO,
    "file": TokenKind.PATH,
    "lang": TokenKind.LANG,
    "sym": TokenKind.SYMBOL,
    "branch": TokenKind.BRANCH,
    "case": TokenKind.CASE,
    None: TokenKind.SUBSTRING,
}


def _load_corpus() -> list[dict[str, Any]]:
    return json.loads(_CORPUS_PATH.read_text())


@pytest.mark.unit
@pytest.mark.parametrize("entry", _load_corpus(), ids=lambda e: e["query"])
def test_python_parses_matches_corpus(entry: dict[str, Any]) -> None:
    try:
        tokenize(entry["query"])
        # tokenize() alone doesn't fully validate (e.g. dangling OR, unbalanced parens are
        # parse()-time errors) -- run the real entrypoint to match `python_parses` exactly.
        from app.query.parser import parse

        parse(entry["query"])
        parses = True
    except QueryParseError:
        parses = False
    assert parses == entry["python_parses"], entry["query"]


_SAFE_ENTRIES = [e for e in _load_corpus() if e["verdict"] == "safe" and e.get("atoms")]


@pytest.mark.unit
@pytest.mark.parametrize("entry", _SAFE_ENTRIES, ids=lambda e: e["query"])
def test_safe_entries_tokenize_to_the_corpus_atoms(entry: dict[str, Any]) -> None:
    tokens = tokenize(entry["query"])
    atoms = entry["atoms"]
    assert len(tokens) == len(atoms), entry["query"]
    for token, atom in zip(tokens, atoms):
        assert token.kind == _FIELD_TO_KIND[atom["field"]], entry["query"]
        assert token.value == atom["value"], entry["query"]
        assert token.position == atom["start"], entry["query"]
