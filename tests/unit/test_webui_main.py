"""Route-level unit tests for the webui FastAPI backend.

No DB, no SDK, no real Lakebase engine: ``get_engine``/``get_settings`` are FastAPI
dependencies (``webui/main.py``), so tests override them (``app.dependency_overrides``) with a
fake engine/connection and a deterministic ``Settings``, then drive requests through a real
``TestClient`` -- pinning the actual HTTP wire shape (status codes, JSON bodies), not just the
underlying payload-builder dicts (those are already pinned by ``tests/unit/test_main.py``).
``service.grep_search``/``service.symbol_search`` are monkeypatched the same way
``test_main.py`` does it, since ``app.service.search_code_payload`` resolves those names from
its OWN module globals regardless of which app calls it.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError

from app import service
from app.config import Settings
from app.search.grep import FileCursor, FileMatches, GrepResult, LineMatch
from webui.main import api_imports, api_references, app, get_engine, get_settings

_NUL_BYTE_ERROR = DataError(
    "SELECT 1", {}, ValueError("PostgreSQL text fields cannot contain NUL (0x00) bytes")
)


def _cfg() -> Settings:
    """A deterministic Settings instance (never reads the real environment)."""
    return Settings(
        lakebase_endpoint=None,
        statement_timeout_ms=5000,
        max_content_bytes=8 * 1024 * 1024,
        row_limit=200,
        max_row_limit=1000,
        semantic_enabled=False,
    )


# --------------------------------------------------------------------------- fake engine


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Minimal Connection stand-in: records SQL and returns canned rows by call order."""

    def __init__(
        self,
        results: list[Any],
        *,
        raise_on_driver_sql: bool = False,
        raise_on_execute: Exception | None = None,
    ) -> None:
        self._results = list(results)
        self._raise_on_driver_sql = raise_on_driver_sql
        self._raise_on_execute = raise_on_execute
        self.driver_sql: list[str] = []

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def begin(self) -> _FakeConn:
        return self

    def exec_driver_sql(self, sql: str) -> None:
        if self._raise_on_driver_sql:
            raise RuntimeError("connection refused")
        self.driver_sql.append(sql)

    def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        if self._raise_on_execute is not None:
            raise self._raise_on_execute
        return self._results.pop(0)


class _FakeEngine:
    def __init__(
        self,
        results: list[Any] | None = None,
        *,
        raise_on_driver_sql: bool = False,
        raise_on_execute: Exception | None = None,
    ) -> None:
        self._conn = _FakeConn(
            results or [],
            raise_on_driver_sql=raise_on_driver_sql,
            raise_on_execute=raise_on_execute,
        )

    def connect(self) -> _FakeConn:
        return self._conn


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _grep_result(*, next_cursor: FileCursor | None = None) -> GrepResult:
    """A fake GrepResult mirroring the golden search fixture (one file, one byte range)."""
    return GrepResult(
        files=(
            FileMatches(
                repo_id=7,
                path="src/handler.go",
                lang="go",
                content_sha="deadbeef",
                branches=("main",),
                line_matches=(
                    LineMatch(line_number=3, line_text="foo lives here", byte_ranges=((0, 3),)),
                ),
            ),
        ),
        truncated=False,
        truncation_reason=None,
        regex_incompatible=False,
        no_content_atom=False,
        zero_width_only_atoms=False,
        next_cursor=next_cursor,
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """A TestClient with the engine/settings dependencies overridden to fakes."""
    engine = _FakeEngine([_FakeResult([_Row(id=7, name="acme/widgets")])])
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    monkeypatch.setattr(service, "symbol_search", lambda *a, **k: None)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


# ------------------------------------------------------------------------------------ /health


@pytest.mark.unit
def test_health_is_always_ok() -> None:
    # No dependency overrides needed: health touches neither engine nor settings.
    with TestClient(app) as test_client:
        resp = test_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ------------------------------------------------------------------------------------- /ready


@pytest.mark.unit
def test_ready_returns_ready_on_success() -> None:
    engine = _FakeEngine()
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/ready")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


@pytest.mark.unit
def test_ready_returns_503_when_probe_fails() -> None:
    engine = _FakeEngine(raise_on_driver_sql=True)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/ready")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503
    assert resp.json() == {"status": "unready"}


# --------------------------------------------------------------------------------- /api/search


@pytest.mark.unit
def test_api_search_wire_shape_includes_next_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = FileCursor(repo_id=7, path="src/handler.go", content_sha="deadbeef")
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep_result(next_cursor=cursor))

    resp = client.get("/api/search", params={"q": "foo"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "foo"
    assert body["file_count"] == 1
    assert body["files"][0]["repo"] == "acme/widgets"
    # Pagination mode (the webui API always supplies cursor=) always carries next_cursor,
    # unlike the bare MCP-tool envelope which omits the key entirely.
    assert "next_cursor" in body
    assert isinstance(body["next_cursor"], str)


@pytest.mark.unit
def test_api_search_exhausted_page_has_null_next_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep_result(next_cursor=None))

    resp = client.get("/api/search", params={"q": "foo"})

    assert resp.status_code == 200
    assert resp.json()["next_cursor"] is None


@pytest.mark.unit
def test_api_search_query_parse_error_is_400(client: TestClient) -> None:
    # service.search_code_payload parses up front and folds any QueryParseError into the payload's
    # query_parse_error field (never raises it out -- see webui/main.py:api_search's docstring), so
    # the route's 400 mapping is driven by that field. `case:` accepts only yes/no, so `case:maybe`
    # is a genuine parse error and no leg ever runs.
    resp = client.get("/api/search", params={"q": "case:maybe"})

    assert resp.status_code == 400
    assert "case:" in resp.json()["detail"]["error"]


@pytest.mark.unit
def test_api_search_bad_cursor_is_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "grep_search", lambda *a, **k: _grep_result())

    resp = client.get("/api/search", params={"q": "foo", "cursor": "not-a-valid-cursor!!"})

    assert resp.status_code == 400
    assert "error" in resp.json()["detail"]


@pytest.mark.unit
def test_api_search_nul_byte_in_cursor_path_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cursor whose decoded path carries a NUL byte passes decode_cursor's shape/type checks
    # (it validates structure, not byte content) but Postgres rejects the NUL once it reaches a
    # bound SQL parameter in the resumed candidate scan -- reproduced here by having the
    # (structurally valid) cursor's resulting grep_search call raise the same DataError that a
    # real Postgres round-trip raises (verified against a live local Postgres: `sqlalchemy.exc
    # .DataError` wrapping psycopg's "PostgreSQL text fields cannot contain NUL (0x00) bytes").
    def _raise(*_a: object, **_k: object) -> GrepResult:
        raise _NUL_BYTE_ERROR

    monkeypatch.setattr(service, "grep_search", _raise)
    cursor = service.encode_cursor(FileCursor(repo_id=1, path="foo\x00bar", content_sha="deadbeef"))

    resp = client.get("/api/search", params={"q": "foo", "cursor": cursor})

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid parameter"


# ----------------------------------------------------------------------------------- /api/file


@pytest.mark.unit
def test_api_file_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # branch=None: three queries -- default_branch lookup, content lookup, then the resolved
    # branch's indexed-commit lookup.
    engine = _FakeEngine(
        [_FakeResult(["HEAD"]), _FakeResult(["print('hi')\n"]), _FakeResult(["abc1234"])]
    )
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/api/file", params={"repo": "acme/widgets", "path": "a.py"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["content"] == "print('hi')\n"
    assert body["branch"] == "HEAD"
    assert body["commit"] == "abc1234"


@pytest.mark.unit
def test_api_file_missing_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # Three queries (default_branch lookup, content lookup, commit lookup); all miss -> None.
    engine = _FakeEngine([_FakeResult([]), _FakeResult([]), _FakeResult([])])
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get(
                "/api/file", params={"repo": "acme/widgets", "path": "missing.py"}
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 404
    body = resp.json()["detail"]
    assert body["repo"] == "acme/widgets"
    assert body["path"] == "missing.py"


@pytest.mark.unit
def test_api_file_forwards_branch_query_param(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /api/file?...&branch=feature forwards branch="feature" to get_file_payload."""
    captured: dict[str, Any] = {}

    def fake_get_file_payload(
        engine: Any, cfg: Any, repo: str, path: str, branch: str | None = None
    ) -> dict[str, Any]:
        captured["branch"] = branch
        return {
            "repo": repo,
            "path": path,
            "branch": branch or "HEAD",
            "content": "x",
            "found": True,
        }

    monkeypatch.setattr(service, "get_file_payload", fake_get_file_payload)
    engine = _FakeEngine()
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get(
                "/api/file", params={"repo": "acme/widgets", "path": "a.py", "branch": "feature"}
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert captured["branch"] == "feature"


@pytest.mark.unit
def test_api_file_omitted_branch_forwards_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting branch forwards None -- unchanged default-branch resolution."""
    captured: dict[str, Any] = {}

    def fake_get_file_payload(
        engine: Any, cfg: Any, repo: str, path: str, branch: str | None = None
    ) -> dict[str, Any]:
        captured["branch"] = branch
        return {"repo": repo, "path": path, "branch": "HEAD", "content": "x", "found": True}

    monkeypatch.setattr(service, "get_file_payload", fake_get_file_payload)
    engine = _FakeEngine()
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/api/file", params={"repo": "acme/widgets", "path": "a.py"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert captured["branch"] is None


@pytest.mark.unit
def test_api_file_nul_byte_in_path_is_400() -> None:
    # repo/path reach a bound SQL parameter in service.get_file_payload's lookup; Postgres
    # rejects a NUL byte there (verified against a live local Postgres -- see
    # test_api_search_nul_byte_in_cursor_path_is_400) rather than simply matching zero rows.
    engine = _FakeEngine(raise_on_execute=_NUL_BYTE_ERROR)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get(
                "/api/file", params={"repo": "acme/widgets", "path": "foo\x00bar"}
            )
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid parameter"


# ---------------------------------------------------------------------------------- /api/repos


@pytest.mark.unit
def test_api_repos_lists_indexed_repos() -> None:
    engine = _FakeEngine(
        [
            _FakeResult(
                [
                    _Row(
                        id=1,
                        name="acme/widgets",
                        default_branch="main",
                        branch="main",
                        last_indexed_commit="deadbeef",
                        last_indexed_at=None,
                    )
                ]
            )
        ]
    )
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/api/repos")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["repos"][0]["name"] == "acme/widgets"
    assert body["repos"][0]["last_indexed_commit"] == "deadbeef"


# ------------------------------------------------------------------------------- /api/semantic


@pytest.mark.unit
def test_api_semantic_disabled_payload_passes_through(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    disabled_payload = {
        "query": "foo",
        "semantic_enabled": False,
        "results": [],
        "count": 0,
        "reason": "semantic search is disabled (set CODE_SEARCH_SEMANTIC_ENABLED=1 to enable)",
    }
    monkeypatch.setattr(service, "semantic_search_payload", lambda *a, **k: disabled_payload)

    resp = client.get("/api/semantic", params={"q": "foo"})

    assert resp.status_code == 200
    assert resp.json() == disabled_payload


@pytest.mark.unit
def test_api_semantic_enabled_payload_passes_through_byte_identical(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    enabled_payload = {
        "query": "how are branch filters compiled to SQL",
        "semantic_enabled": True,
        "backend": "standin",
        "results": [
            {
                "repo": "acme/widgets",
                "file": "src/handler.go",
                "chunk_index": 0,
                "content": "func Handle() {}",
                "rrf_score": 0.0164,
            }
        ],
        "count": 1,
    }
    monkeypatch.setattr(service, "semantic_search_payload", lambda *a, **k: enabled_payload)

    resp = client.get("/api/semantic", params={"q": "how are branch filters compiled to SQL"})

    assert resp.status_code == 200
    assert resp.json() == enabled_payload


@pytest.mark.unit
def test_api_semantic_new_filter_and_similarity_fields_pass_through_unmodified(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The route body is unchanged for these fields: it
    # never inspects similarity/query_parse_error/unsupported_filter/nothing_to_embed, it just
    # forwards whatever app.service returns. Three separate payload shapes, one per new
    # recoverable state plus the enabled+similarity shape, each asserted byte-identical.
    similarity_payload = {
        "query": "repo:acme/widgets how are branch filters compiled to SQL",
        "semantic_enabled": True,
        "backend": "standin",
        "results": [
            {
                "repo": "acme/widgets",
                "file": "src/handler.go",
                "chunk_index": 0,
                "content": "func Handle() {}",
                "rrf_score": 0.0164,
                "similarity": 0.812,
            },
            {
                "repo": "acme/widgets",
                "file": "src/legacy.go",
                "chunk_index": 1,
                "content": "func Legacy() {}",
                "rrf_score": 0.011,
                "similarity": None,
            },
        ],
        "count": 2,
    }
    parse_error_payload = {
        "query": "repo:",
        "semantic_enabled": True,
        "results": [],
        "count": 0,
        "query_parse_error": "empty value for filter 'repo:'",
    }
    unsupported_filter_payload = {
        "query": "commit:deadbeef",
        "semantic_enabled": True,
        "results": [],
        "count": 0,
        "unsupported_filter": "commit:",
        "reason": "commit: is not a semantic filter; use search_code for commit-scoped lookups",
    }
    nothing_to_embed_payload = {
        "query": "repo:acme/widgets",
        "semantic_enabled": True,
        "results": [],
        "count": 0,
        "nothing_to_embed": True,
        "reason": "the query has no text left to embed after filters were removed",
    }

    for payload in (
        similarity_payload,
        parse_error_payload,
        unsupported_filter_payload,
        nothing_to_embed_payload,
    ):
        monkeypatch.setattr(service, "semantic_search_payload", lambda *a, **k: payload)

        resp = client.get("/api/semantic", params={"q": payload["query"] or "foo"})

        assert resp.status_code == 200
        assert resp.json() == payload


@pytest.mark.unit
def test_api_semantic_limit_default_and_clamp_and_branch_threading(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # limit=50 default (MCP-tool parity, NOT /api/search's 0 -> row_limit convention); an
    # oversized explicit limit still clamps through service.clamp_limit; branch threads through
    # unchanged (None when omitted).
    calls: list[tuple[str, int, str | None]] = []

    def _fake(_engine: Any, _cfg: Any, q: str, limit: int, branch: str | None) -> dict[str, Any]:
        calls.append((q, limit, branch))
        return {
            "query": q,
            "semantic_enabled": True,
            "backend": "standin",
            "results": [],
            "count": 0,
        }

    monkeypatch.setattr(service, "semantic_search_payload", _fake)

    client.get("/api/semantic", params={"q": "foo"})
    client.get("/api/semantic", params={"q": "foo", "limit": 5000})
    client.get("/api/semantic", params={"q": "foo", "branch": "main"})

    assert calls[0] == ("foo", 50, None)
    assert calls[1] == ("foo", 1000, None)  # clamped to _cfg()'s max_row_limit
    assert calls[2] == ("foo", 50, "main")


@pytest.mark.unit
def test_api_semantic_missing_q_is_422(client: TestClient) -> None:
    resp = client.get("/api/semantic")

    assert resp.status_code == 422


@pytest.mark.unit
def test_api_semantic_data_error_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*_a: object, **_k: object) -> dict[str, Any]:
        raise _NUL_BYTE_ERROR

    monkeypatch.setattr(service, "semantic_search_payload", _raise)

    resp = client.get("/api/semantic", params={"q": "foo\x00bar"})

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid parameter"


@pytest.mark.unit
def test_api_semantic_backend_failure_is_502_and_does_not_leak_error_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*_a: object, **_k: object) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "semantic_search_payload", _raise)

    resp = client.get("/api/semantic", params={"q": "foo"})

    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "semantic search backend unavailable"
    assert "boom" not in resp.text


# ------------------------------------------------------------------------ /api/semantic/status


@pytest.mark.unit
def test_api_semantic_status_false_by_default() -> None:
    app.dependency_overrides[get_settings] = _cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/api/semantic/status")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == {"semantic_enabled": False}


@pytest.mark.unit
def test_api_semantic_status_true_when_enabled() -> None:
    def _enabled_cfg() -> Settings:
        return Settings(
            lakebase_endpoint=None,
            statement_timeout_ms=5000,
            max_content_bytes=8 * 1024 * 1024,
            row_limit=200,
            max_row_limit=1000,
            semantic_enabled=True,
        )

    app.dependency_overrides[get_settings] = _enabled_cfg
    try:
        with TestClient(app) as test_client:
            resp = test_client.get("/api/semantic/status")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json() == {"semantic_enabled": True}


# ---------------------------------------------------------------------------- /api/references


@pytest.mark.unit
def test_api_references_payload_passes_through_byte_identical(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "query": "process",
        "kind": "references",
        "symbol": "process",
        "branch": None,
        "query_too_broad": False,
        "sites": [
            {
                "repo": "acme/widgets",
                "file": "src/handler.go",
                "line": 10,
                "edge_kind": "call",
                "target_name": "process",
                "enclosing_symbol": {"name": "Handle", "kind": "function"},
                "resolution": "ambiguous",
                "candidate_count": 2,
                "candidates_truncated": False,
                "candidates": [
                    {
                        "repo": "acme/widgets",
                        "file": "src/proc.go",
                        "line": 4,
                        "name": "process",
                        "kind": "function",
                        "same_repo": True,
                        "same_file": False,
                        "kind_match": True,
                    },
                    {
                        "repo": "acme/other",
                        "file": "lib/proc.go",
                        "line": 9,
                        "name": "process",
                        "kind": "function",
                        "same_repo": False,
                        "same_file": False,
                        "kind_match": True,
                    },
                ],
            }
        ],
        "site_count": 1,
        "resolution_summary": {"unique": 0, "ambiguous": 1, "unresolved": 0},
        "truncated": False,
        "truncation_reason": None,
    }
    monkeypatch.setattr(service, "find_references_payload", lambda *a, **k: payload)

    resp = client.get("/api/references", params={"symbol": "process"})

    assert resp.status_code == 200
    assert resp.json() == payload


@pytest.mark.unit
def test_api_references_limit_default_and_clamp_and_branch_threading(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int, str | None]] = []

    def _fake(
        _engine: Any, _cfg: Any, name: str, limit: int, branch: str | None = None
    ) -> dict[str, Any]:
        calls.append((name, limit, branch))
        return {
            "query": name,
            "kind": "references",
            "symbol": name,
            "branch": branch,
            "query_too_broad": False,
            "sites": [],
            "site_count": 0,
            "resolution_summary": {"unique": 0, "ambiguous": 0, "unresolved": 0},
            "truncated": False,
            "truncation_reason": None,
        }

    monkeypatch.setattr(service, "find_references_payload", _fake)

    client.get("/api/references", params={"symbol": "process"})
    client.get("/api/references", params={"symbol": "process", "limit": 0})
    client.get("/api/references", params={"symbol": "process", "limit": 5000})
    client.get("/api/references", params={"symbol": "process", "branch": "feature/x"})

    assert calls[0] == ("process", 200, None)
    assert calls[1] == ("process", 200, None)  # 0 -> cfg.row_limit (200)
    assert calls[2] == ("process", 1000, None)  # clamped to cfg.max_row_limit
    assert calls[3] == ("process", 200, "feature/x")

    # Silent drift between the route's and the MCP tool's `limit` default would break AC2
    # (same defaulted call must return the same result set) without any test noticing --
    # pin the default itself, not just its observed clamped value.
    from app import main as mcp_main

    route_default = inspect.signature(api_references).parameters["limit"].default
    mcp_default = inspect.signature(mcp_main.find_references).parameters["limit"].default
    assert route_default == mcp_default == 200


@pytest.mark.unit
def test_api_references_missing_symbol_is_422(client: TestClient) -> None:
    resp = client.get("/api/references")

    assert resp.status_code == 422


@pytest.mark.unit
def test_api_references_nul_byte_is_400(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(*_a: object, **_k: object) -> dict[str, Any]:
        raise _NUL_BYTE_ERROR

    monkeypatch.setattr(service, "find_references_payload", _raise)

    resp = client.get("/api/references", params={"symbol": "foo\x00bar"})

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid parameter"


# -------------------------------------------------------------------------------- /api/imports


@pytest.mark.unit
def test_api_imports_payload_passes_through_byte_identical(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "query": "acme/widgets",
        "kind": "imports",
        "direction": "imports",
        "repo": "acme/widgets",
        "repo_known": True,
        "target": None,
        "branch": None,
        "query_too_broad": False,
        "sites": [
            {
                "repo": "acme/widgets",
                "file": "src/handler.py",
                "line": 1,
                "edge_kind": "import",
                "target_name": "os.path",
                "enclosing_symbol": None,
                "resolution": "unresolved",
                "candidate_count": 0,
                "candidates_truncated": False,
                "candidates": [],
            }
        ],
        "site_count": 1,
        "resolution_summary": {"unique": 0, "ambiguous": 0, "unresolved": 1},
        "truncated": False,
        "truncation_reason": None,
    }
    monkeypatch.setattr(service, "list_imports_payload", lambda *a, **k: payload)

    resp = client.get("/api/imports", params={"repo": "acme/widgets"})

    assert resp.status_code == 200
    assert resp.json() == payload


@pytest.mark.unit
def test_api_imports_validation_payloads_pass_through_as_200(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Proves D2's "no payload inspection" for /api/imports: every PRE-DB structured validation
    # payload (app.service._list_imports_error_payload) round-trips as a 200 body, byte-identical,
    # never re-shaped or gated by this route.
    unsupported_direction_payload = {
        "query": "",
        "kind": "imports",
        "direction": "sideways",
        "repo": None,
        "repo_known": True,
        "target": None,
        "branch": None,
        "query_too_broad": False,
        "sites": [],
        "site_count": 0,
        "resolution_summary": {"unique": 0, "ambiguous": 0, "unresolved": 0},
        "truncated": False,
        "truncation_reason": None,
        "unsupported_direction": "sideways",
        "reason": "direction must be one of 'imports' or 'imported_by'",
    }
    missing_repo_payload = {
        "query": "",
        "kind": "imports",
        "direction": "imports",
        "repo": None,
        "repo_known": True,
        "target": None,
        "branch": None,
        "query_too_broad": False,
        "sites": [],
        "site_count": 0,
        "resolution_summary": {"unique": 0, "ambiguous": 0, "unresolved": 0},
        "truncated": False,
        "truncation_reason": None,
        "missing_repo": True,
        "reason": "direction='imports' requires a repo to enumerate; pass repo=",
    }
    missing_target_payload = {
        "query": "",
        "kind": "imports",
        "direction": "imported_by",
        "repo": None,
        "repo_known": True,
        "target": None,
        "branch": None,
        "query_too_broad": False,
        "sites": [],
        "site_count": 0,
        "resolution_summary": {"unique": 0, "ambiguous": 0, "unresolved": 0},
        "truncated": False,
        "truncation_reason": None,
        "missing_target": True,
        "reason": "direction='imported_by' requires a target dotted path; pass target=",
    }

    for payload in (unsupported_direction_payload, missing_repo_payload, missing_target_payload):
        monkeypatch.setattr(service, "list_imports_payload", lambda *a, _p=payload, **k: _p)

        resp = client.get("/api/imports", params={"direction": payload["direction"]})

        assert resp.status_code == 200
        assert resp.json() == payload


@pytest.mark.unit
def test_api_imports_limit_default_clamp_and_arg_threading(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str | None, int, str | None, str | None, str]] = []

    def _fake(
        _engine: Any,
        _cfg: Any,
        repo: str | None,
        limit: int,
        branch: str | None = None,
        *,
        target: str | None = None,
        direction: str = "imports",
    ) -> dict[str, Any]:
        calls.append((repo, limit, branch, target, direction))
        return {
            "query": repo or target or "",
            "kind": "imports",
            "direction": direction,
            "repo": repo,
            "repo_known": True,
            "target": target,
            "branch": branch,
            "query_too_broad": False,
            "sites": [],
            "site_count": 0,
            "resolution_summary": {"unique": 0, "ambiguous": 0, "unresolved": 0},
            "truncated": False,
            "truncation_reason": None,
        }

    monkeypatch.setattr(service, "list_imports_payload", _fake)

    client.get("/api/imports", params={"repo": "acme/widgets"})
    client.get("/api/imports", params={"repo": "acme/widgets", "limit": 0})
    client.get("/api/imports", params={"repo": "acme/widgets", "limit": 5000})
    client.get(
        "/api/imports",
        params={
            "target": "os.path",
            "direction": "imported_by",
            "repo": "acme/widgets",
            "branch": "feature/x",
        },
    )

    assert calls[0] == ("acme/widgets", 200, None, None, "imports")
    assert calls[1] == ("acme/widgets", 200, None, None, "imports")  # 0 -> cfg.row_limit (200)
    assert calls[2] == ("acme/widgets", 1000, None, None, "imports")  # clamped
    assert calls[3] == ("acme/widgets", 200, "feature/x", "os.path", "imported_by")

    from app import main as mcp_main

    route_default = inspect.signature(api_imports).parameters["limit"].default
    mcp_default = inspect.signature(mcp_main.list_imports).parameters["limit"].default
    assert route_default == mcp_default == 200


@pytest.mark.unit
def test_api_imports_nul_byte_is_400(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> dict[str, Any]:
        raise _NUL_BYTE_ERROR

    monkeypatch.setattr(service, "list_imports_payload", _raise)

    resp = client.get("/api/imports", params={"repo": "foo\x00bar"})

    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid parameter"


# --------------------------------------------------------------------- security headers


@pytest.mark.unit
def test_security_headers_present_on_every_response() -> None:
    # The middleware wraps ALL responses (API and SPA alike) -- proven here on /health, which
    # takes no dependency overrides, so this test can't accidentally pass because of DB fakery.
    with TestClient(app) as test_client:
        resp = test_client.get("/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"


@pytest.mark.unit
def test_security_headers_present_on_error_response() -> None:
    with TestClient(app) as test_client:
        resp = test_client.get("/api/does-not-exist")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"


# ------------------------------------------------------------------- unmatched /api/* paths


@pytest.mark.unit
def test_unknown_api_path_is_json_404_not_spa_shell() -> None:
    # Before the SPAStaticFiles fix, any /api/* path not matched by a registered router fell
    # through to the SPA mount, which served index.html with a 200 -- an API caller expecting
    # JSON got an HTML document instead of a 404.
    with TestClient(app) as test_client:
        resp = test_client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


@pytest.mark.unit
def test_unknown_non_api_path_still_falls_back_to_spa_shell() -> None:
    # A client-side route (e.g. a deep link to /file?repo=x&path=y, or any path the SPA router
    # owns) is not a real file on disk; it must still get the SPA shell, not a bare 404 -- the
    # /api/ exclusion in SPAStaticFiles must not regress this.
    with TestClient(app) as test_client:
        resp = test_client.get("/some/client-side/route")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
