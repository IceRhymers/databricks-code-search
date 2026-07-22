"""Unit tests for the keyset-cursor pagination layer in ``app/service.py``.

No DB: cursor encode/decode is pure, and the pagination-mode gating inside
``search_code_payload`` is driven with the same fake engine/connection + fake ``GrepResult``
harness as ``tests/unit/test_main.py``. Real multi-page walks against Postgres (disjoint
pages, mixed sym+content, exhaustion, byte/row-cap semantics) live in the integration suite,
which is the only place a real keyset SQL predicate can be exercised.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import service
from app.config import Settings
from app.query.parser import parse
from app.search.errors import QueryTooBroadError
from app.search.grep import FileCursor, FileMatches, GrepResult, LineMatch
from app.search.symbols import SymbolMatch, SymbolResult

# --------------------------------------------------------------------------- fixtures


def _cfg() -> Settings:
    return Settings(
        lakebase_endpoint=None,
        statement_timeout_ms=5000,
        max_content_bytes=8 * 1024 * 1024,
        row_limit=200,
        max_row_limit=1000,
        semantic_enabled=False,
    )


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.driver_sql: list[str] = []

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeConn:
        return self

    def exec_driver_sql(self, sql: str) -> None:
        self.driver_sql.append(sql)

    def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        return self._results.pop(0)


class _FakeEngine:
    def __init__(self, results: list[Any]) -> None:
        self._conn = _FakeConn(results)

    def connect(self) -> _FakeConn:
        return self._conn


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _grep(
    *,
    files: tuple[FileMatches, ...] = (),
    truncated: bool = False,
    truncation_reason: str | None = None,
    regex_incompatible: bool = False,
    no_content_atom: bool = False,
    zero_width_only_atoms: bool = False,
    next_cursor: FileCursor | None = None,
) -> GrepResult:
    return GrepResult(
        files=files,
        truncated=truncated,
        truncation_reason=truncation_reason,
        regex_incompatible=regex_incompatible,
        no_content_atom=no_content_atom,
        zero_width_only_atoms=zero_width_only_atoms,
        next_cursor=next_cursor,
    )


def _no_sym() -> SymbolResult:
    return SymbolResult(symbols=(), truncated=False, truncation_reason=None, no_symbol_atom=True)


def _one_sym(*, line: int = 2) -> SymbolResult:
    return SymbolResult(
        symbols=(
            SymbolMatch(
                repo_id=7,
                path="src/handler.go",
                lang="go",
                content_sha="deadbeef",
                branches=("main",),
                name="Handler",
                kind="function",
                start_line=line,
            ),
        ),
        truncated=False,
        truncation_reason=None,
        no_symbol_atom=False,
    )


# --------------------------------------------------------------- cursor encode/decode (pure)


@pytest.mark.unit
def test_cursor_round_trips() -> None:
    original = FileCursor(repo_id=42, path="src/handler.go", content_sha="deadbeef")
    encoded = service.encode_cursor(original)
    assert isinstance(encoded, str)
    assert service.decode_cursor(encoded) == original


@pytest.mark.unit
def test_cursor_is_opaque_base64url_no_padding() -> None:
    # No raw '=' padding chars and no '+'/'/' (base64url, not base64) leaking into the wire
    # value -- it must survive being embedded in a URL query string unescaped.
    encoded = service.encode_cursor(FileCursor(repo_id=1, path="a/b.py", content_sha="deadbeef"))
    assert "=" not in encoded
    assert "+" not in encoded
    assert "/" not in encoded


@pytest.mark.unit
@pytest.mark.parametrize(
    "garbage",
    [
        "not-base64-json!!!",
        "",
        "e30",  # base64url("{}") -- valid JSON, missing v/r/p
    ],
)
def test_decode_cursor_rejects_garbage(garbage: str) -> None:
    with pytest.raises(service.CursorError):
        service.decode_cursor(garbage)


@pytest.mark.unit
def test_decode_cursor_rejects_wrong_version() -> None:
    import base64
    import json

    payload = json.dumps({"v": 2, "r": 1, "p": "a.py"})
    tampered = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    with pytest.raises(service.CursorError):
        service.decode_cursor(tampered)


@pytest.mark.unit
def test_decode_cursor_rejects_tampered_bytes() -> None:
    encoded = service.encode_cursor(FileCursor(repo_id=1, path="a.py", content_sha="deadbeef"))
    tampered = encoded[:-1] + ("A" if encoded[-1] != "A" else "B")
    with pytest.raises(service.CursorError):
        service.decode_cursor(tampered)


@pytest.mark.unit
def test_decode_cursor_rejects_bool_repo_id() -> None:
    # bool is an int subclass in Python; a tampered {"r": true, ...} must not silently coerce.
    import base64
    import json

    payload = json.dumps({"v": 1, "r": True, "p": "a.py"})
    tampered = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    with pytest.raises(service.CursorError):
        service.decode_cursor(tampered)


# ---------------------------------------------------------- pagination-mode envelope shape


@pytest.mark.unit
def test_bare_call_omits_next_cursor_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # No `cursor` kwarg at all -- the legacy shape, byte-identical to before pagination was added.
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep())
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: _no_sym())

    payload = service.search_code_payload(_FakeEngine([]), _cfg(), "foo", 50)
    assert "next_cursor" not in payload


@pytest.mark.unit
def test_page_one_cursor_none_includes_next_cursor_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep())
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: _no_sym())

    payload = service.search_code_payload(_FakeEngine([]), _cfg(), "foo", 50, cursor=None)
    assert "next_cursor" in payload
    assert payload["next_cursor"] is None  # exhausted -- grep.GrepResult.next_cursor was None


@pytest.mark.unit
def test_pagination_mode_encodes_grep_next_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    file_cursor = FileCursor(repo_id=7, path="src/handler.go", content_sha="deadbeef")
    monkeypatch.setattr(
        service, "grep_search", lambda *a, **k: _grep(truncated=False, next_cursor=file_cursor)
    )
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: _no_sym())

    payload = service.search_code_payload(_FakeEngine([]), _cfg(), "foo", 50, cursor=None)
    assert payload["next_cursor"] == service.encode_cursor(file_cursor)
    assert payload["truncated"] is False  # row-cap suppression is grep's job; forwarded as-is


@pytest.mark.unit
def test_garbled_cursor_raises_not_silently_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    # decode_cursor runs BEFORE any engine/connection use, so a fake engine never gets touched;
    # this pins that a bad cursor is a hard failure, not a silent "treat as page 1".
    monkeypatch.setattr(
        service, "grep_search", lambda *a, **k: pytest.fail("grep_search must not run")
    )
    with pytest.raises(service.CursorError):
        service.search_code_payload(_FakeEngine([]), _cfg(), "foo", 50, cursor="garbage!!!")


# ----------------------------------------------------------------- symbol leg page-1-only


@pytest.mark.unit
def test_page_one_runs_symbol_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _spy_symbol_search(*_a: object, **_k: object) -> SymbolResult:
        calls.append("called")
        return _one_sym()

    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep())
    monkeypatch.setattr(service, "symbol_search", _spy_symbol_search)
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])

    payload = service.search_code_payload(engine, _cfg(), "sym:Handler foo", 50, cursor=None)
    assert calls == ["called"]
    assert payload["file_count"] == 1


@pytest.mark.unit
def test_continuation_page_skips_symbol_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    def _spy_symbol_search(*_a: object, **_k: object) -> SymbolResult:
        pytest.fail("symbol_search must not run on a continuation page")

    monkeypatch.setattr(
        service,
        "grep_search",
        lambda *a, **k: _grep(
            files=(
                FileMatches(
                    repo_id=7,
                    path="src/handler.go",
                    lang="go",
                    content_sha="deadbeef",
                    branches=("main",),
                    line_matches=(
                        LineMatch(line_number=3, line_text="foo", byte_ranges=((0, 3),)),
                    ),
                ),
            )
        ),
    )
    monkeypatch.setattr(service, "symbol_search", _spy_symbol_search)
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])

    cursor = service.encode_cursor(
        FileCursor(repo_id=7, path="src/handler.go", content_sha="deadbeef")
    )
    payload = service.search_code_payload(engine, _cfg(), "sym:Handler foo", 50, cursor=cursor)
    assert payload["file_count"] == 1
    # No symbol entry folded in on this page.
    (file,) = payload["files"]
    assert all("symbols" not in m for m in file["matches"])


@pytest.mark.unit
def test_continuation_page_suppresses_no_content_atom_for_sym_only_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `sym:Handler` alone is structurally filter-only to grep on EVERY page. Page 1 would
    # suppress no_content_atom because the (skipped-here) symbol leg answers; a continuation
    # page must reach the same suppressed answer via the structural check, not report a
    # spurious no_content_atom just because the leg didn't run.
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep(no_content_atom=True))
    monkeypatch.setattr(
        service, "symbol_search", lambda *a, **k: pytest.fail("must not run on continuation page")
    )

    cursor = service.encode_cursor(
        FileCursor(repo_id=7, path="src/handler.go", content_sha="deadbeef")
    )
    payload = service.search_code_payload(_FakeEngine([]), _cfg(), "sym:Handler", 50, cursor=cursor)
    assert payload["no_content_atom"] is False


@pytest.mark.unit
def test_continuation_page_without_sym_atom_reports_no_content_atom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Contrast case: a filter-only query with NO sym: atom at all must still flag
    # no_content_atom on a continuation page (there is no leg, live or skipped, to suppress it).
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep(no_content_atom=True))
    monkeypatch.setattr(
        service, "symbol_search", lambda *a, **k: pytest.fail("must not run on continuation page")
    )

    cursor = service.encode_cursor(
        FileCursor(repo_id=7, path="docs/readme.md", content_sha="deadbeef")
    )
    payload = service.search_code_payload(_FakeEngine([]), _cfg(), "file:.md", 50, cursor=cursor)
    assert payload["no_content_atom"] is True


# ---------------------------------------------------- negative-only cursor suppression (#70)


@pytest.mark.unit
def test_negative_only_page_one_suppresses_next_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fully-negated query (e.g. `-foo`) is no_content_atom=True at the grep layer exactly like
    # a filter-only query, and the SAME suppression applies: even if grep's own candidate scan
    # row-capped (a rare term excluded from a huge corpus can still match >= row_limit files) and
    # produced a non-null next_cursor, page 1 forces it back to None -- there is nothing to
    # highlight on any later page either, so continuing would just replay empty pages forever.
    file_cursor = FileCursor(repo_id=7, path="src/handler.go", content_sha="deadbeef")
    monkeypatch.setattr(
        service,
        "grep_search",
        lambda *a, **k: _grep(no_content_atom=True, truncated=True, next_cursor=file_cursor),
    )
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: _no_sym())

    payload = service.search_code_payload(_FakeEngine([]), _cfg(), "-foo", 50, cursor=None)
    assert payload["next_cursor"] is None
    assert payload["no_content_atom"] is True


# ------------------------------------------------------------------------------- misc gating


@pytest.mark.unit
def test_query_too_broad_on_grep_still_sets_next_cursor_null_in_pagination_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_a: object, **_k: object) -> GrepResult:
        raise QueryTooBroadError("too broad")

    monkeypatch.setattr(service, "grep_search", _raise)
    payload = service.search_code_payload(_FakeEngine([]), _cfg(), "foo", 50, cursor=None)
    assert payload["query_too_broad"] is True
    assert payload["next_cursor"] is None


@pytest.mark.unit
def test_query_too_broad_on_grep_omits_next_cursor_when_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_a: object, **_k: object) -> GrepResult:
        raise QueryTooBroadError("too broad")

    monkeypatch.setattr(service, "grep_search", _raise)
    payload = service.search_code_payload(_FakeEngine([]), _cfg(), "foo", 50)
    assert "next_cursor" not in payload


# ------------------------------------------------- permalink_branch selection


@pytest.mark.unit
def test_collect_branch_filters_finds_top_level_branch_atom() -> None:
    assert service._collect_branch_filters(parse("branch:main foo")) == frozenset({"main"})


@pytest.mark.unit
def test_collect_branch_filters_no_branch_atom_is_empty() -> None:
    assert service._collect_branch_filters(parse("foo lang:go")) == frozenset()


@pytest.mark.unit
def test_collect_branch_filters_collects_across_nested_and_or() -> None:
    node = parse("branch:a (branch:b OR lang:go)")
    assert service._collect_branch_filters(node) == frozenset({"a", "b"})


@pytest.mark.unit
def test_select_permalink_branch_smallest_of_intersection() -> None:
    result = service._select_permalink_branch(frozenset({"main", "release"}), ("main", "release"))
    assert result == "main"


@pytest.mark.unit
def test_select_permalink_branch_empty_intersection_falls_back_to_min_row_branch() -> None:
    result = service._select_permalink_branch(frozenset({"x"}), ("main", "feature"))
    assert result == "feature"
    assert result in ("main", "feature")


@pytest.mark.unit
def test_select_permalink_branch_no_filters_is_none() -> None:
    assert service._select_permalink_branch(frozenset(), ("main",)) is None


@pytest.mark.unit
def test_select_permalink_branch_no_row_branches_is_none() -> None:
    assert service._select_permalink_branch(frozenset({"x"}), ()) is None


# --------------------------------------------------------------- commit: filter collectors


@pytest.mark.unit
def test_collect_commit_filters_gathers_prefixes() -> None:
    assert service._collect_commit_filters(parse("commit:abc1234")) == frozenset({"abc1234"})
    assert service._collect_commit_filters(parse("commit:aaaaaaa OR commit:bbbbbbb")) == frozenset(
        {"aaaaaaa", "bbbbbbb"}
    )


@pytest.mark.unit
def test_collect_branch_filters_is_benign_on_commit_filter() -> None:
    # A commit: atom carries no branch: value; the branch collector skips it (never raises).
    assert service._collect_branch_filters(parse("commit:abc1234")) == frozenset()


@pytest.mark.unit
def test_has_content_atom_distinguishes_commit_moods() -> None:
    # Bare / filter-only commit queries carry no content atom (reverse-lookup mood)...
    assert service._has_content_atom(parse("commit:abc1234")) is False
    assert service._has_content_atom(parse("commit:abc1234 file:src/")) is False
    # ...while a content term (or a sym: atom) flips it to the scoped-search mood.
    assert service._has_content_atom(parse("commit:abc1234 foo")) is True
    assert service._has_content_atom(parse("commit:abc1234 sym:Handler")) is True


# ----------------------------------------------------------------- negation (Not) collectors


@pytest.mark.unit
def test_collect_branch_filters_skips_negated_branch() -> None:
    # A `-branch:x` is an exclusion, not a selection: collecting x here would corrupt
    # permalink-branch selection and fire a spurious repo_branches lookup. So it is skipped.
    assert service._collect_branch_filters(parse("-branch:main foo")) == frozenset()
    # An affirmative branch alongside a negated one still collects only the affirmative value.
    assert service._collect_branch_filters(parse("branch:main -branch:dev")) == frozenset({"main"})


@pytest.mark.unit
def test_collect_commit_filters_skips_negated_commit() -> None:
    assert service._collect_commit_filters(parse("-commit:abc1234 foo")) == frozenset()
    assert service._collect_commit_filters(parse("commit:abc1234 -commit:bbbbbbb")) == frozenset(
        {"abc1234"}
    )


@pytest.mark.unit
def test_has_content_atom_recurses_into_negation() -> None:
    # A negated content atom is still content-bearing: `commit:abc -foo` is a SCOPED search
    # (excluding foo), not a bare reverse-lookup.
    assert service._has_content_atom(parse("commit:abc1234 -foo")) is True
    assert service._has_content_atom(parse("-foo")) is True
    # A filter alongside a negated content atom is content-bearing too, not just a commit scope.
    assert service._has_content_atom(parse("lang:go -foo")) is True
    # A negated pure-filter (no content leaf under the Not) does not manufacture a content atom.
    assert service._has_content_atom(parse("commit:abc1234 -file:src/")) is False


@pytest.mark.unit
def test_query_has_symbol_atom_excludes_negated_symbol() -> None:
    # A `-sym:foo`-only query does NOT count as "has a symbol atom" (the symbol leg cannot
    # answer it), staying consistent with the symbols collector skipping exclusions.
    assert service._query_has_symbol_atom(parse("-sym:foo")) is False
    # A positive sym: alongside a negated one still counts.
    assert service._query_has_symbol_atom(parse("-sym:foo sym:bar")) is True
