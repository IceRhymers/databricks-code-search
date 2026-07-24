"""Unit tests for the MCP response-shaping pipeline (byte budget, projection, truncation,
resume handles) added in app/main.py -- the "MCP response shaping" section between
``_signals`` and ``_dispatch``.

No DB, no SDK: pure-function tests for ``project_for_mcp``/``_effective_budget``/``_fit_list``/
``_fit_lines``/the per-tool truncators/``_shape_response``/``_shape_get_file_response``, plus a
few fake-engine integration tests for the ``search_code`` cursor-synthesis path (which needs a
name->id SELECT) and the full async tool wrappers. Complements (never duplicates)
``tests/unit/test_main.py``'s zoekt-parity pins: those assert the UNCHANGED service-layer
payload shape; these assert the MCP-only shaping layered on top.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from typing import Any

import pytest

from app import main, service
from app.config import Settings
from app.search.grep import FileCursor, FileMatches, GrepResult, LineMatch
from app.search.symbols import SymbolMatch, SymbolResult
from tests.unit.test_main import (
    _cfg,
    _FakeEngine,
    _FakeLifespanContext,
    _FakeResult,
    _no_sym,
    _Row,
)

# --------------------------------------------------------------------------- _effective_budget


@pytest.mark.unit
@pytest.mark.parametrize(
    "max_bytes,expected",
    [
        (None, 100_000),
        (0, 100_000),
        (-5, 100_000),
        (5_000, 5_000),
        (100_000, 100_000),
        (500_000, 100_000),  # over the env max clamps DOWN
        (10, 1024),  # under the floor clamps UP to _MIN_MAX_BYTES
    ],
)
def test_effective_budget_clamp_matrix(max_bytes: int | None, expected: int) -> None:
    cfg = Settings(lakebase_endpoint=None, mcp_max_response_bytes=100_000)
    assert main._effective_budget(max_bytes, cfg) == expected


# --------------------------------------------------------------------------------- _fit_list


@pytest.mark.unit
def test_fit_list_exactness() -> None:
    items = [{"a": 1}, {"a": 2}, {"a": 3}]
    costs = [len(json.dumps(item)) + 2 for item in items]

    assert main._fit_list(items, costs[0] + costs[1]) == 2
    assert main._fit_list(items, costs[0] + costs[1] - 1) == 1
    assert main._fit_list(items, 0) == 0
    assert main._fit_list(items, sum(costs)) == 3
    assert main._fit_list([], 100) == 0


# ------------------------------------------------------------------------------- _fit_lines


@pytest.mark.unit
def test_fit_lines_newline_cost_term() -> None:
    # cost("abc") = len(json.dumps("abc")) - 2 = 3 (the escaped body, excluding the two quotes
    # json.dumps adds); each line after the first adds 2 more for the re-appended "\n".
    assert main._line_cost("abc") == 3
    lines = ["abc", "de"]
    assert main._fit_lines(lines, 3) == 1  # only the first line fits
    assert main._fit_lines(lines, 6) == 1  # 3 + (2 + 2) = 7 > 6, still only the first
    assert main._fit_lines(lines, 7) == 2  # exactly fits both
    assert main._fit_lines([], 100) == 0


# --------------------------------------------------------------------------- project_for_mcp


@pytest.mark.unit
def test_projection_drops_exactly_the_three_fields() -> None:
    payload = {
        "query": "foo",
        "duration_ns": 123,
        "files": [
            {
                "repo": "acme/widgets",
                "file": "f.py",
                "content_sha": "deadbeef",
                "permalink_branch": "main",
                "matches": [{"line": 1, "text": "foo", "byte_ranges": [[0, 3]]}],
            }
        ],
    }
    main.project_for_mcp("search_code", payload)
    assert "duration_ns" not in payload
    assert "content_sha" not in payload["files"][0]
    assert "byte_ranges" not in payload["files"][0]["matches"][0]
    # Everything else survives untouched.
    assert payload["files"][0]["repo"] == "acme/widgets"
    assert payload["files"][0]["permalink_branch"] == "main"
    assert payload["files"][0]["matches"][0]["text"] == "foo"


@pytest.mark.unit
def test_projection_is_noop_for_non_search_code_tools() -> None:
    for tool in ("list_repos", "find_references", "list_imports", "semantic_search", "get_file"):
        payload = {"repos": [{"name": "r"}], "count": 1}
        before = copy.deepcopy(payload)
        main.project_for_mcp(tool, payload)
        assert payload == before


@pytest.mark.unit
def test_projection_is_none_safe_on_minimal_payloads() -> None:
    # The wrapper tests' fakes return skeletal dicts like {"query": q}; projection must no-op
    # rather than KeyError/TypeError.
    payload: dict[str, Any] = {"query": "foo"}
    main.project_for_mcp("search_code", payload)
    assert payload == {"query": "foo"}

    payload2: dict[str, Any] = {"files": [{"repo": "r"}]}  # no matches key at all
    main.project_for_mcp("search_code", payload2)
    assert payload2 == {"files": [{"repo": "r"}]}


# ---------------------------------------------------------------- static tail-trim truncators


@pytest.mark.unit
def test_list_repos_truncator_trims_tail_and_recomputes_count() -> None:
    payload = {
        "repos": [{"name": f"org/repo-{i}", "branches": ["main"]} for i in range(50)],
        "count": 50,
    }
    main._TRUNCATORS["list_repos"](payload, 500)
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "token_budget"
    assert payload["count"] == len(payload["repos"])
    assert 0 < payload["count"] < 50
    assert len(json.dumps(payload)) <= 500


@pytest.mark.unit
def test_list_repos_truncator_none_safe_on_minimal_payload() -> None:
    payload: dict[str, Any] = {"query": "x"}
    main._TRUNCATORS["list_repos"](payload, 10)
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "token_budget"


def _reference_site(index: int, *, resolution: str = "unique") -> dict[str, Any]:
    return {
        "repo": "org/repo",
        "file": f"src/site_{index}.py",
        "line": index,
        "edge_kind": "call",
        "target_name": "Handler",
        "enclosing_symbol": {"name": f"func_{index}", "kind": "function"},
        "resolution": resolution,
        "candidate_count": 1,
        "candidates_truncated": False,
        "candidates": [
            {
                "repo": "org/repo",
                "file": "src/handler.py",
                "line": 1,
                "name": "Handler",
                "kind": "function",
                "same_repo": True,
                "same_file": False,
                "kind_match": True,
            }
        ],
    }


@pytest.mark.unit
@pytest.mark.parametrize("tool", ["find_references", "list_imports"])
def test_reference_sites_truncator_recomputes_summary(tool: str) -> None:
    sites = [_reference_site(i) for i in range(300)]
    payload = {
        "sites": sites,
        "site_count": len(sites),
        "resolution_summary": {"unique": len(sites), "ambiguous": 0, "unresolved": 0},
        "truncated": False,
        "truncation_reason": None,
    }
    main._TRUNCATORS[tool](payload, 2000)
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "token_budget"
    assert payload["site_count"] == len(payload["sites"])
    assert payload["resolution_summary"]["unique"] == len(payload["sites"])
    assert payload["resolution_summary"]["ambiguous"] == 0
    assert len(json.dumps(payload)) <= 2000


@pytest.mark.unit
def test_reference_sites_truncator_none_safe_on_minimal_payload() -> None:
    payload: dict[str, Any] = {"query": "Handler"}
    main._TRUNCATORS["find_references"](payload, 10)
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "token_budget"


@pytest.mark.unit
def test_semantic_results_truncator_drops_least_relevant_tail() -> None:
    results = [
        {
            "repo": "org/repo",
            "file": f"src/f{i}.py",
            "chunk_index": i,
            "content": "x" * 200,
            "start_line": 1,
            "end_line": 10,
            "rrf_score": 1.0 / (i + 1),
            "similarity": 0.9,
        }
        for i in range(100)
    ]
    payload = {"query": "auth flow", "semantic_enabled": True, "results": results, "count": 100}
    main._TRUNCATORS["semantic_search"](payload, 3000)
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "token_budget"
    assert payload["count"] == len(payload["results"])
    assert payload["count"] < 100
    # Tail-trim: the surviving prefix is exactly the most-relevant leading results.
    assert [r["chunk_index"] for r in payload["results"]] == list(range(payload["count"]))
    assert len(json.dumps(payload)) <= 3000


@pytest.mark.unit
def test_semantic_results_truncator_none_safe_on_minimal_payload() -> None:
    payload: dict[str, Any] = {"query": "x"}
    main._TRUNCATORS["semantic_search"](payload, 10)
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "token_budget"


# -------------------------------------------------------------------------- _shape_response


@pytest.mark.unit
def test_shape_response_under_budget_is_a_noop_pass_through() -> None:
    payload = {"repos": [{"name": "r"}], "count": 1}
    body, log_fields = main._shape_response(
        "list_repos", payload, 100_000, main._TRUNCATORS["list_repos"]
    )
    out = json.loads(body)
    assert out == {"repos": [{"name": "r"}], "count": 1}
    assert log_fields["pre_bytes"] == log_fields["post_bytes"]


@pytest.mark.unit
def test_shape_response_returns_flagged_irreducible_envelope_without_raising() -> None:
    # No truncator (e.g. an unregistered tool name): never crash, just pass through even if
    # over budget -- Principle 4, never hard-error.
    payload = {"query": "x" * 10_000}
    body, _ = main._shape_response("mystery_tool", payload, 100, None)
    assert json.loads(body)["query"] == "x" * 10_000


@pytest.mark.unit
def test_signals_log_includes_duration_ns_before_projection() -> None:
    payload = {"duration_ns": 123456, "files": []}
    body, log_fields = main._shape_response("search_code", payload, 100_000, None)
    assert log_fields["signals"]["duration_ns"] == 123456
    assert '"duration_ns"' not in body


# ------------------------------------------------------------------------------ cursor_invalid


@pytest.mark.unit
def test_cursor_invalid_payload_shape() -> None:
    error = service.CursorError("malformed pagination cursor: 'garbled'")
    payload = main._cursor_invalid_payload("foo", error)
    assert payload["cursor_invalid"] is True
    assert "malformed pagination cursor" in payload["reason"]
    assert payload["files"] == []
    assert payload["next_cursor"] is None
    assert payload["truncated"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_code_tool_returns_cursor_invalid_payload_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_a: object, **_k: object) -> dict[str, Any]:
        raise service.CursorError("malformed pagination cursor: 'garbled'")

    monkeypatch.setattr(main, "_search_code_payload", _raise)
    ctx = _FakeLifespanContext(_FakeEngine([]), _cfg())

    out = await main.search_code("foo", ctx, cursor="garbled")  # type: ignore[arg-type]

    payload = json.loads(out)
    assert payload["cursor_invalid"] is True
    assert payload["files"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_code_tool_preserves_row_cap_pagination_signal_under_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Under budget, MCP shaping must not interfere with the builder's own pagination signal:
    # a plain grep row-cap fill in pagination mode is truncated=False + a real next_cursor.
    def _fake_payload(
        engine: Any, cfg: Settings, query: str, limit: int, cursor: str | None = None
    ) -> dict[str, Any]:
        return {
            "query": query,
            "files": [],
            "file_count": 0,
            "match_count": 0,
            "truncated": False,
            "truncation_reason": None,
            "next_cursor": "opaquetoken",
        }

    monkeypatch.setattr(main, "_search_code_payload", _fake_payload)
    ctx = _FakeLifespanContext(_FakeEngine([]), _cfg())

    out = await main.search_code("foo", ctx)  # type: ignore[arg-type]

    payload = json.loads(out)
    assert payload["truncated"] is False
    assert payload["next_cursor"] == "opaquetoken"


# ------------------------------------------------------------- pinned MCP envelope (post-proj)


@pytest.mark.unit
def test_mcp_search_code_envelope_shape_is_pinned_minus_dropped_plus_cursor() -> None:
    payload = {
        "query": "foo",
        "file_count": 1,
        "match_count": 1,
        "duration_ns": 999,
        "files": [
            {
                "repo": "r",
                "file": "f.py",
                "language": "python",
                "branches": ["main"],
                "matches": [{"line": 1, "text": "foo", "byte_ranges": [[0, 3]]}],
                "content_sha": "sha",
                "permalink_branch": None,
            }
        ],
        "truncated": False,
        "truncation_reason": None,
        "regex_incompatible": False,
        "regex_invalid": None,
        "query_too_broad": False,
        "query_parse_error": None,
        "no_content_atom": False,
        "zero_width_only_atoms": False,
        "next_cursor": None,
    }
    body, _ = main._shape_response("search_code", payload, 100_000, None)
    out = json.loads(body)

    assert "duration_ns" not in out
    assert set(out) == {
        "query",
        "file_count",
        "match_count",
        "files",
        "truncated",
        "truncation_reason",
        "regex_incompatible",
        "regex_invalid",
        "query_too_broad",
        "query_parse_error",
        "no_content_atom",
        "zero_width_only_atoms",
        "next_cursor",
    }
    (file_entry,) = out["files"]
    assert "content_sha" not in file_entry
    assert set(file_entry) == {
        "repo",
        "file",
        "language",
        "branches",
        "matches",
        "permalink_branch",
    }
    (match,) = file_entry["matches"]
    assert "byte_ranges" not in match
    assert set(match) == {"line", "text"}


# -------------------------------------------------------------------------------- get_file


def _get_file_payload_fixture(content: str, *, found: bool = True) -> dict[str, Any]:
    return {
        "repo": "acme/widgets",
        "path": "big.txt",
        "branch": "main",
        "content": content if found else None,
        "found": found,
        "commit": "abc1234" if found else None,
    }


@pytest.mark.unit
def test_get_file_miss_never_truncates() -> None:
    payload = _get_file_payload_fixture("irrelevant", found=False)
    body, _ = main._shape_get_file_response(payload, 1, 100_000)
    out = json.loads(body)
    assert out["truncated"] is False
    assert out["truncation_reason"] is None
    assert out["next_start_line"] is None
    assert out["start_line"] == 1
    assert out["found"] is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        "\n".join(f"line {i} " + "x" * 50 for i in range(2000)),
        "\n".join(f"line {i}\r" for i in range(2000)),  # CRLF-shaped: trailing \r per line
        "\n".join(f"line {i}" for i in range(500)) + "\nlast line no trailing newline",
        "\n".join(f"line {i} \x0c tail" for i in range(500)),  # form feed
        "\n".join(f"line {i}    tail" for i in range(500)),  # U+2028 / U+2029
    ],
    ids=["plain", "crlf", "no-trailing-newline", "form-feed", "unicode-seps"],
)
def test_get_file_traversal_reassembles_exact_content(content: str) -> None:
    budget = 2000  # small enough to force many pages
    start_line = 1
    pages: list[str] = []
    guard = 0
    while True:
        guard += 1
        assert guard < 5000, "traversal did not terminate"
        payload = _get_file_payload_fixture(content)
        body, _ = main._shape_get_file_response(payload, start_line, budget)
        assert len(body) <= budget
        page = json.loads(body)
        pages.append(page["content"])
        if page["next_start_line"] is None:
            break
        assert page["next_start_line"] > start_line
        start_line = page["next_start_line"]
    assert "\n".join(pages) == content


@pytest.mark.unit
def test_get_file_line_numbering_matches_split_not_splitlines() -> None:
    # grep.py:436 numbers lines via content.split("\n"); str.splitlines() would (wrongly, for
    # this purpose) also break on \x0c/ / , diverging from search_code's line numbers.
    content = "a\x0cb\nc d\ne"
    assert len(content.splitlines()) > len(content.split("\n"))

    payload = _get_file_payload_fixture(content)
    body, _ = main._shape_get_file_response(payload, 1, 100_000)
    out = json.loads(body)
    assert out["content"] == content
    assert out["next_start_line"] is None
    assert out["truncated"] is False


@pytest.mark.unit
def test_get_file_single_oversized_line_returned_flagged_over_budget() -> None:
    huge_line = "x" * 5000
    content = f"short\n{huge_line}\nshort2"
    payload = _get_file_payload_fixture(content)

    body, _ = main._shape_get_file_response(payload, 2, 1000)  # page starting at the huge line

    out = json.loads(body)
    assert out["truncated"] is True
    assert out["truncation_reason"] == "token_budget"
    assert out["content"] == huge_line  # progress guarantee: >= 1 line always returned
    assert len(body) > 1000  # documented edge: exceeds the budget for this one call
    assert out["next_start_line"] == 3


@pytest.mark.unit
def test_get_file_start_line_clamps_below_one() -> None:
    content = "a\nb\nc"
    payload = _get_file_payload_fixture(content)
    body, _ = main._shape_get_file_response(payload, 0, 100_000)
    out = json.loads(body)
    assert out["start_line"] == 1
    assert out["content"] == content


@pytest.mark.unit
def test_get_file_start_line_past_eof_returns_empty_untruncated() -> None:
    content = "a\nb\nc"
    payload = _get_file_payload_fixture(content)
    body, _ = main._shape_get_file_response(payload, 100, 100_000)
    out = json.loads(body)
    assert out["content"] == ""
    assert out["truncated"] is False
    assert out["next_start_line"] is None


# ------------------------------------------------------ search_code cursor synthesis (D3)


class _ScalarOnly:
    def __init__(self, value: int | None) -> None:
        self._value = value

    def scalar_one_or_none(self) -> int | None:
        return self._value


class _FakeSearchConn:
    """Understands exactly the two SELECT shapes search_code_payload/_resolve_repo_id issue:
    ``select(Repo.id, Repo.name)`` (the repo-name map) and ``select(Repo.id).where(Repo.name ==
    ...)`` (the truncation-time cursor-synthesis lookup) -- dispatched by column name rather
    than a real dialect/DB, using SQLAlchemy's own compiled-bind-param introspection."""

    def __init__(self, name_by_id: dict[int, str], *, raise_on_id_lookup: bool = False) -> None:
        self._name_by_id = name_by_id
        self._id_by_name = {name: repo_id for repo_id, name in name_by_id.items()}
        self._raise_on_id_lookup = raise_on_id_lookup
        self.driver_sql: list[str] = []

    def __enter__(self) -> _FakeSearchConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeSearchConn:
        return self

    def exec_driver_sql(self, sql: str) -> None:
        self.driver_sql.append(sql)

    def execute(self, stmt: Any) -> Any:
        cols = list(stmt.selected_columns.keys())
        if cols == ["id", "name"]:
            rows = [_Row(id=repo_id, name=name) for repo_id, name in self._name_by_id.items()]
            return _FakeResult(rows)
        if cols == ["id"]:
            if self._raise_on_id_lookup:
                raise RuntimeError("simulated lookup fault")
            params = stmt.compile().params
            name = next(iter(params.values()))
            return _ScalarOnly(self._id_by_name.get(name))
        raise AssertionError(f"unexpected query shape: {cols}")


class _FakeSearchEngine:
    def __init__(self, name_by_id: dict[int, str], *, raise_on_id_lookup: bool = False) -> None:
        self._conn = _FakeSearchConn(name_by_id, raise_on_id_lookup=raise_on_id_lookup)

    def connect(self) -> _FakeSearchConn:
        return self._conn


def _make_grep_stub(all_files: list[FileMatches]) -> Callable[..., GrepResult]:
    """A cursor-aware service.grep_search stand-in: returns every candidate strictly after
    ``cursor`` in (repo_id, path, content_sha) order, UNCAPPED (no row_cap) -- so byte-budget
    truncation at the MCP layer is the only truncation source these tests exercise.
    """
    ordered = sorted(all_files, key=lambda f: (f.repo_id, f.path, f.content_sha))

    def _grep(
        conn: Any, query: str, *, cursor: FileCursor | None = None, **_kwargs: Any
    ) -> GrepResult:
        if cursor is None:
            remaining = ordered
        else:
            remaining = [
                f
                for f in ordered
                if (f.repo_id, f.path, f.content_sha)
                > (cursor.repo_id, cursor.path, cursor.content_sha)
            ]
        return GrepResult(
            files=tuple(remaining),
            truncated=False,
            truncation_reason=None,
            regex_incompatible=False,
            no_content_atom=False,
            zero_width_only_atoms=False,
            next_cursor=None,
        )

    return _grep


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_code_cursor_traversal_equals_uncapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    all_files = [
        FileMatches(
            repo_id=7,
            path=f"file{i}.py",
            lang="python",
            content_sha=f"sha{i}",
            branches=("main",),
            line_matches=(LineMatch(1, f"needle in file {i}", ((0, 6),)),),
        )
        for i in range(10)
    ]
    monkeypatch.setattr(service, "grep_search", _make_grep_stub(all_files))
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: _no_sym())
    engine = _FakeSearchEngine({7: "acme/widgets"})
    ctx = _FakeLifespanContext(engine, _cfg())

    # Page 1: a small max_bytes forces MCP-level (pure tail-trim) truncation.
    out1 = await main.search_code("needle", ctx, max_bytes=1200)  # type: ignore[arg-type]
    page1 = json.loads(out1)
    assert page1["truncated"] is True
    assert page1["truncation_reason"] == "token_budget"
    page1_paths = {f["file"] for f in page1["files"]}
    assert page1_paths, "page 1 must keep at least one file (progress guarantee)"
    assert "file9.py" not in page1_paths  # lexicographically last -> tail-dropped
    cursor = page1["next_cursor"]
    assert cursor is not None

    # Page 2: a generous budget lets the (already smaller) remainder fit in one page.
    out2 = await main.search_code(  # type: ignore[arg-type]
        "needle", ctx, cursor=cursor, max_bytes=1_000_000
    )
    page2 = json.loads(out2)
    page2_paths = {f["file"] for f in page2["files"]}

    assert page1_paths | page2_paths == {f"file{i}.py" for i in range(10)}
    assert not (page1_paths & page2_paths)  # no double-counting across pages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_code_single_oversized_file_progress_guarantee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for the blocking defect found in review: a single file whose OWN serialized
    # size already exceeds the budget must still come back as exactly that one file (never an
    # empty, unresumable `files: []` with `next_cursor: null`), flagged truncated, with a
    # next_cursor that lets a caller advance past it -- mirroring get_file's single-oversized-
    # line edge. Under the OLD code, _fit_list returned keep=0 here, `next_cursor` stayed None,
    # and the response was a silent, permanent dead end.
    huge_file = FileMatches(
        repo_id=7,
        path="file0_huge.py",
        lang="python",
        content_sha="sha-huge",
        branches=("main",),
        line_matches=tuple(
            LineMatch(i, f"needle match number {i} " + "x" * 40, ((0, 6),)) for i in range(1, 501)
        ),
    )
    small_files = [
        FileMatches(
            repo_id=7,
            path=f"file{i}_small.py",
            lang="python",
            content_sha=f"sha-small-{i}",
            branches=("main",),
            line_matches=(LineMatch(1, f"needle in small file {i}", ((0, 6),)),),
        )
        for i in range(1, 3)
    ]
    all_files = [huge_file, *small_files]
    monkeypatch.setattr(service, "grep_search", _make_grep_stub(all_files))
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: _no_sym())
    engine = _FakeSearchEngine({7: "acme/widgets"})
    ctx = _FakeLifespanContext(engine, _cfg())

    # A budget far smaller than the huge file's own serialized size (the file's ~500 matches
    # alone serialize to well over 10x this) but the file still must come back, not be dropped.
    out1 = await main.search_code("needle", ctx, max_bytes=2000)  # type: ignore[arg-type]
    page1 = json.loads(out1)

    assert page1["truncated"] is True
    assert page1["truncation_reason"] == "token_budget"
    assert [f["file"] for f in page1["files"]] == ["file0_huge.py"]  # exactly 1 file kept
    assert len(out1) > 2000  # the documented edge: this one response exceeds the budget
    cursor = page1["next_cursor"]
    assert cursor is not None, "progress guarantee: a resume cursor must still be synthesized"

    # Traversal past the oversized file must work: the remaining (small) files come back.
    out2 = await main.search_code(  # type: ignore[arg-type]
        "needle", ctx, cursor=cursor, max_bytes=100_000
    )
    page2 = json.loads(out2)
    assert {f["file"] for f in page2["files"]} == {"file1_small.py", "file2_small.py"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_code_cursor_no_row_degrades_without_cursor_or_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The builder's own str(repo_id) fallback (service.py:764) means the payload's `repo`
    # string may match no `repos.name` row -- the truncator must degrade to a flagged,
    # handle-less truncation rather than raising.
    all_files = [
        FileMatches(
            repo_id=7,
            path=f"file{i}.py",
            lang="python",
            content_sha=f"sha{i}",
            branches=("main",),
            line_matches=(LineMatch(1, f"needle in file {i}", ((0, 6),)),),
        )
        for i in range(10)
    ]
    monkeypatch.setattr(service, "grep_search", _make_grep_stub(all_files))
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: _no_sym())
    # No name in the map at all -> _repo_name_map falls back to str(repo_id) for every file,
    # so _resolve_repo_id's name lookup can never find a row.
    engine = _FakeSearchEngine({})
    ctx = _FakeLifespanContext(engine, _cfg())

    out = await main.search_code("needle", ctx, max_bytes=1200)  # type: ignore[arg-type]
    payload = json.loads(out)

    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "token_budget"
    assert payload["next_cursor"] is None  # degraded: no cursor synthesized, never raised


@pytest.mark.unit
@pytest.mark.asyncio
async def test_search_code_cursor_resolve_fault_degrades_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    all_files = [
        FileMatches(
            repo_id=7,
            path=f"file{i}.py",
            lang="python",
            content_sha=f"sha{i}",
            branches=("main",),
            line_matches=(LineMatch(1, f"needle in file {i}", ((0, 6),)),),
        )
        for i in range(10)
    ]
    monkeypatch.setattr(service, "grep_search", _make_grep_stub(all_files))
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: _no_sym())
    engine = _FakeSearchEngine({7: "acme/widgets"}, raise_on_id_lookup=True)
    ctx = _FakeLifespanContext(engine, _cfg())

    out = await main.search_code("needle", ctx, max_bytes=1200)  # type: ignore[arg-type]
    payload = json.loads(out)

    assert payload["truncated"] is True
    assert payload["next_cursor"] is None  # the id lookup faulted -- degrade, never raise


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mixed_grep_symbol_truncation_traversal(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC3's documented carve-out: the symbol leg folds in page-1-only. When byte-budget
    # truncation drops the tail file carrying a symbol match, that symbol is lost from the
    # traversal (never reappears on a continuation page), while CONTENT matches for every file
    # -- including the one that lost its symbol -- are still recoverable losslessly via cursor.
    all_files = [
        FileMatches(
            repo_id=7,
            path=f"file{i}.py",
            lang="python",
            content_sha=f"sha{i}",
            branches=("main",),
            line_matches=(LineMatch(1, f"needle in file {i}", ((0, 6),)),),
        )
        for i in range(10)
    ]
    monkeypatch.setattr(service, "grep_search", _make_grep_stub(all_files))
    monkeypatch.setattr(
        service,
        "symbol_search",
        lambda *a, **k: SymbolResult(
            symbols=(
                SymbolMatch(
                    repo_id=7,
                    path="file9.py",  # lexicographically last -> guaranteed tail-dropped
                    lang="python",
                    content_sha="sha9",
                    branches=("main",),
                    name="Handler",
                    kind="function",
                    start_line=1,
                ),
            ),
            truncated=False,
            truncation_reason=None,
            no_symbol_atom=False,
        ),
    )
    engine = _FakeSearchEngine({7: "acme/widgets"})
    ctx = _FakeLifespanContext(engine, _cfg())

    out1 = await main.search_code(  # type: ignore[arg-type]
        "needle sym:Handler", ctx, max_bytes=1500
    )
    page1 = json.loads(out1)
    assert page1["truncated"] is True
    page1_paths = {f["file"] for f in page1["files"]}
    assert "file9.py" not in page1_paths

    def _has_symbols(payload: dict[str, Any]) -> bool:
        return any(
            "symbols" in match for file_entry in payload["files"] for match in file_entry["matches"]
        )

    assert _has_symbols(page1) is False  # dropped before it could even be considered

    cursor = page1["next_cursor"]
    assert cursor is not None
    out2 = await main.search_code(  # type: ignore[arg-type]
        "needle sym:Handler", ctx, cursor=cursor, max_bytes=1_000_000
    )
    page2 = json.loads(out2)
    page2_paths = {f["file"] for f in page2["files"]}

    # Content matches: fully recoverable across the two pages, including file9's own content.
    assert page1_paths | page2_paths == {f"file{i}.py" for i in range(10)}
    # The symbol match itself never reappears -- continuation pages skip the symbol leg
    # entirely (page-1-only folding), so it is genuinely and permanently lost.
    assert _has_symbols(page2) is False


# --------------------------------------------------------------------- AC2: budget respected


def _worst_case_payload(tool: str) -> dict[str, Any]:
    if tool == "list_repos":
        return {
            "repos": [
                {
                    "name": f"org/{'x' * 40}-repo-{i}",
                    "branches": ["main", "develop", "release/1.0"],
                    "index_time": "2026-07-18T00:00:00+00:00",
                    "default_branch": "main",
                    "last_indexed_commit": "a" * 40,
                    "branch_details": [
                        {
                            "branch": "main",
                            "last_indexed_commit": "a" * 40,
                            "index_time": "2026-07-18T00:00:00+00:00",
                        }
                    ],
                }
                for i in range(500)
            ],
            "count": 500,
        }
    if tool in ("find_references", "list_imports"):
        sites = []
        for i in range(200):
            candidates = [
                {
                    "repo": f"org/repo-{j}",
                    "file": f"src/module_{j}.py",
                    "line": j,
                    "name": "Handler",
                    "kind": "function",
                    "same_repo": j % 2 == 0,
                    "same_file": False,
                    "kind_match": True,
                }
                for j in range(32)
            ]
            sites.append(
                {
                    "repo": "org/repo",
                    "file": f"src/site_{i}.py",
                    "line": i,
                    "edge_kind": "call" if tool == "find_references" else "import",
                    "target_name": "Handler",
                    "enclosing_symbol": {"name": f"func_{i}", "kind": "function"},
                    "resolution": "ambiguous",
                    "candidate_count": 32,
                    "candidates_truncated": False,
                    "candidates": candidates,
                }
            )
        base: dict[str, Any] = {
            "query": "Handler",
            "sites": sites,
            "site_count": 200,
            "resolution_summary": {"unique": 0, "ambiguous": 200, "unresolved": 0},
            "truncated": False,
            "truncation_reason": None,
            "query_too_broad": False,
        }
        if tool == "find_references":
            base.update({"kind": "references", "symbol": "Handler", "branch": None})
        else:
            base.update(
                {
                    "kind": "imports",
                    "direction": "imports",
                    "repo": "org/repo",
                    "repo_known": True,
                    "target": None,
                    "branch": None,
                }
            )
        return base
    if tool == "semantic_search":
        return {
            "query": "how does auth work",
            "semantic_enabled": True,
            "results": [
                {
                    "repo": "org/repo",
                    "file": f"src/file_{i}.py",
                    "chunk_index": i,
                    "content": "def handler():\n    pass\n" * 20,
                    "start_line": 1,
                    "end_line": 40,
                    "rrf_score": 0.01,
                    "similarity": 0.9,
                }
                for i in range(200)
            ],
            "count": 200,
        }
    if tool == "search_code":
        files = [
            {
                "repo": "org/repo",
                "file": f"src/file_{i}.py",
                "language": "python",
                "branches": ["main"],
                "matches": [
                    {"line": j, "text": "match text " * 5, "byte_ranges": [[0, 5], [10, 15]]}
                    for j in range(10)
                ],
                "content_sha": "a" * 40,
                "permalink_branch": "main",
            }
            for i in range(200)
        ]
        return {
            "query": "handler",
            "file_count": 200,
            "match_count": 2000,
            "duration_ns": 123456,
            "files": files,
            "truncated": False,
            "truncation_reason": None,
            "regex_incompatible": False,
            "regex_invalid": None,
            "query_too_broad": False,
            "query_parse_error": None,
            "no_content_atom": False,
            "zero_width_only_atoms": False,
            "next_cursor": None,
        }
    if tool == "get_file":
        content = "\n".join(f"line {i} " + "x" * 60 for i in range(20_000))
        return {
            "repo": "org/repo",
            "path": "big.py",
            "branch": "main",
            "content": content,
            "found": True,
            "commit": "a" * 40,
        }
    raise ValueError(tool)


@pytest.mark.unit
@pytest.mark.parametrize("max_bytes", [None, 5000])
@pytest.mark.parametrize(
    "tool",
    ["list_repos", "find_references", "list_imports", "semantic_search", "search_code", "get_file"],
)
def test_all_tools_respect_budget(tool: str, max_bytes: int | None) -> None:
    cfg = Settings(lakebase_endpoint=None, mcp_max_response_bytes=100_000)
    budget = main._effective_budget(max_bytes, cfg)
    payload = _worst_case_payload(tool)

    if tool == "get_file":
        body, _ = main._shape_get_file_response(payload, 1, budget)
    elif tool == "search_code":
        truncator = main._make_search_code_truncator(_FakeSearchEngine({}), cfg)
        body, _ = main._shape_response(tool, payload, budget, truncator)
    else:
        body, _ = main._shape_response(tool, payload, budget, main._TRUNCATORS[tool])

    assert len(body) <= budget


# ------------------------------------------------------------------- AC4: Lane-3 fixture


def _lane3_search_code_fixture(
    n_files: int = 200, n_matches: int = 6, n_ranges: int = 6
) -> dict[str, Any]:
    """A synthetic search_code payload sized like the trace's measured shape (dense matches,
    each carrying content_sha/byte_ranges/duration_ns) -- ported as a deterministic fixture
    rather than depending on the scratchpad measure_tokens.py script."""
    files = []
    for i in range(n_files):
        matches = [
            {
                "line": j,
                "text": "x" * 24,
                "byte_ranges": [[k, k + 3] for k in range(n_ranges)],
            }
            for j in range(n_matches)
        ]
        files.append(
            {
                "repo": "org/repo",
                "file": f"src/file_{i}.py",
                "language": "python",
                "branches": ["main"],
                "matches": matches,
                "content_sha": "a" * 40,
                "permalink_branch": "main",
            }
        )
    return {
        "query": "handler",
        "file_count": n_files,
        "match_count": n_files * n_matches,
        "duration_ns": 123456,
        "files": files,
        "truncated": False,
        "truncation_reason": None,
        "regex_incompatible": False,
        "regex_invalid": None,
        "query_too_broad": False,
        "query_parse_error": None,
        "no_content_atom": False,
        "zero_width_only_atoms": False,
        "next_cursor": None,
    }


@pytest.mark.unit
def test_lane3_fixture_reduction_at_least_25pct() -> None:
    payload = _lane3_search_code_fixture()
    original_size = len(json.dumps(payload))

    projected = copy.deepcopy(payload)
    main.project_for_mcp("search_code", projected)
    projected_size = len(json.dumps(projected))

    reduction = 1 - (projected_size / original_size)
    assert reduction >= 0.25, f"projection-only reduction was {reduction:.1%}, need >= 25%"
