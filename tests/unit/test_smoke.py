"""Unit tests for the pure predicate functions in ``scripts/smoke.py``.

``scripts/`` is not an importable package, so the module is loaded by path (the same idiom
``tests/integration/test_migrations.py`` uses for ``migrate.py``). Only the I/O-free
predicates are exercised here; the live HTTP / SQL / MCP legs are covered by the manual E2E
checklist, not CI.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_SMOKE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "smoke.py"


def _load_smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_under_test", _SMOKE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()


# zoekt-parity envelope (mirrors the golden shape in tests/unit/test_main.py).
GOLDEN_PAYLOAD = {
    "query": "foo",
    "file_count": 1,
    "match_count": 2,
    "duration_ns": 123,
    "files": [
        {
            "repo": "acme/widgets",
            "file": "src/handler.go",
            "language": "go",
            "branches": ["HEAD"],
            "matches": [
                {
                    "line": 3,
                    "text": "// foo lives here and foo again",
                    "byte_ranges": [[3, 6], [24, 27]],
                }
            ],
        }
    ],
    "truncated": False,
    "truncation_reason": None,
    "regex_incompatible": False,
    "query_too_broad": False,
    "query_parse_error": None,
}


# --- health_ok / ready_ok ---------------------------------------------------


@pytest.mark.unit
def test_health_ok_true_for_status_ok() -> None:
    assert smoke.health_ok({"status": "ok"}) is True


@pytest.mark.unit
@pytest.mark.parametrize("body", [{}, {"status": "unready"}, {"status": "OK"}, {"other": 1}])
def test_health_ok_false_otherwise(body: dict) -> None:
    assert smoke.health_ok(body) is False


@pytest.mark.unit
def test_ready_ok_only_200() -> None:
    assert smoke.ready_ok(200) is True
    for code in (0, 204, 301, 401, 500, 503):
        assert smoke.ready_ok(code) is False


# --- assert_connectivity / assert_corpus_nonempty ---------------------------


@pytest.mark.unit
def test_assert_connectivity_pass_on_one() -> None:
    assert smoke.assert_connectivity(1).ok is True


@pytest.mark.unit
@pytest.mark.parametrize("scalar", [0, None, 2, "1"])
def test_assert_connectivity_fail_otherwise(scalar: object) -> None:
    assert smoke.assert_connectivity(scalar).ok is False


@pytest.mark.unit
def test_assert_corpus_nonempty_boundary() -> None:
    assert smoke.assert_corpus_nonempty(1).ok is True
    assert smoke.assert_corpus_nonempty(42).ok is True
    assert smoke.assert_corpus_nonempty(0).ok is False


# --- validate_search_payload (zoekt parity + negatives) ---------------------


@pytest.mark.unit
def test_validate_search_payload_accepts_golden() -> None:
    assert smoke.validate_search_payload(GOLDEN_PAYLOAD).ok is True


@pytest.mark.unit
def test_validate_search_payload_rejects_zero_file_count() -> None:
    payload = {**GOLDEN_PAYLOAD, "file_count": 0, "files": []}
    assert smoke.validate_search_payload(payload).ok is False


@pytest.mark.unit
def test_validate_search_payload_rejects_empty_files() -> None:
    payload = {**GOLDEN_PAYLOAD, "files": []}
    assert smoke.validate_search_payload(payload).ok is False


@pytest.mark.unit
def test_validate_search_payload_rejects_file_without_matches() -> None:
    bad_file = {**GOLDEN_PAYLOAD["files"][0], "matches": []}
    payload = {**GOLDEN_PAYLOAD, "files": [bad_file]}
    assert smoke.validate_search_payload(payload).ok is False


@pytest.mark.unit
def test_validate_search_payload_rejects_match_missing_line() -> None:
    bad_match = {"text": "x", "byte_ranges": [[0, 1]]}
    bad_file = {**GOLDEN_PAYLOAD["files"][0], "matches": [bad_match]}
    payload = {**GOLDEN_PAYLOAD, "files": [bad_file]}
    assert smoke.validate_search_payload(payload).ok is False


@pytest.mark.unit
def test_validate_search_payload_rejects_match_non_list_byte_ranges() -> None:
    bad_match = {"line": 3, "text": "x", "byte_ranges": "nope"}
    bad_file = {**GOLDEN_PAYLOAD["files"][0], "matches": [bad_match]}
    payload = {**GOLDEN_PAYLOAD, "files": [bad_file]}
    assert smoke.validate_search_payload(payload).ok is False


# --- validate_references_payload (shape-only + negatives) -------------------


# find_references-shaped golden: one ambiguous site with two ranked candidates.
REFERENCES_GOLDEN = {
    "query": "process",
    "kind": "references",
    "symbol": "process",
    "branch": None,
    "query_too_broad": False,
    "site_count": 1,
    "sites": [
        {
            "repo": "acme/widgets",
            "file": "src/caller.py",
            "line": 5,
            "edge_kind": "call",
            "target_name": "process",
            "enclosing_symbol": {"name": "run", "kind": "function"},
            "resolution": "ambiguous",
            "candidate_count": 2,
            "candidates_truncated": False,
            "candidates": [
                {"repo": "acme/widgets", "file": "src/service.py", "line": 10, "name": "process"},
                {"repo": "acme/widgets", "file": "src/worker.py", "line": 4, "name": "process"},
            ],
        }
    ],
    "resolution_summary": {"unique": 0, "ambiguous": 1, "unresolved": 0},
    "truncated": False,
    "truncation_reason": None,
}

# list_imports-shaped golden: carries `direction`, zero sites (accepted by design).
IMPORTS_GOLDEN = {
    "query": "acme/widgets",
    "kind": "imports",
    "direction": "imports",
    "repo": "acme/widgets",
    "repo_known": True,
    "target": None,
    "branch": None,
    "query_too_broad": False,
    "site_count": 0,
    "sites": [],
    "resolution_summary": {"unique": 0, "ambiguous": 0, "unresolved": 0},
    "truncated": False,
    "truncation_reason": None,
}


@pytest.mark.unit
def test_validate_references_payload_accepts_references_golden() -> None:
    assert smoke.validate_references_payload(REFERENCES_GOLDEN).ok is True


@pytest.mark.unit
def test_validate_references_payload_accepts_imports_golden_with_direction() -> None:
    assert smoke.validate_references_payload(IMPORTS_GOLDEN).ok is True


@pytest.mark.unit
def test_validate_references_payload_accepts_zero_sites() -> None:
    # A live corpus's symbols are unpredictable, so an empty-but-well-formed envelope is a PASS.
    payload = {**REFERENCES_GOLDEN, "site_count": 0, "sites": []}
    assert smoke.validate_references_payload(payload).ok is True


@pytest.mark.unit
def test_validate_references_payload_rejects_site_count_mismatch() -> None:
    payload = {**REFERENCES_GOLDEN, "site_count": 5}
    assert smoke.validate_references_payload(payload).ok is False


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["truncated", "query_too_broad"])
def test_validate_references_payload_rejects_non_bool_flags(flag: str) -> None:
    payload = {**REFERENCES_GOLDEN, flag: "nope"}
    assert smoke.validate_references_payload(payload).ok is False


@pytest.mark.unit
def test_validate_references_payload_rejects_wrong_resolution_summary_keys() -> None:
    payload = {**REFERENCES_GOLDEN, "resolution_summary": {"unique": 0, "ambiguous": 1}}
    assert smoke.validate_references_payload(payload).ok is False


@pytest.mark.unit
def test_validate_references_payload_rejects_non_int_resolution_summary_values() -> None:
    payload = {
        **REFERENCES_GOLDEN,
        "resolution_summary": {"unique": "0", "ambiguous": 1, "unresolved": 0},
    }
    assert smoke.validate_references_payload(payload).ok is False


@pytest.mark.unit
def test_validate_references_payload_rejects_non_list_sites() -> None:
    payload = {**REFERENCES_GOLDEN, "sites": "nope", "site_count": 0}
    assert smoke.validate_references_payload(payload).ok is False


@pytest.mark.unit
def test_validate_references_payload_rejects_site_missing_file() -> None:
    bad_site = {**REFERENCES_GOLDEN["sites"][0]}
    del bad_site["file"]
    payload = {**REFERENCES_GOLDEN, "sites": [bad_site]}
    assert smoke.validate_references_payload(payload).ok is False


@pytest.mark.unit
def test_validate_references_payload_rejects_site_non_int_line() -> None:
    bad_site = {**REFERENCES_GOLDEN["sites"][0], "line": "5"}
    payload = {**REFERENCES_GOLDEN, "sites": [bad_site]}
    assert smoke.validate_references_payload(payload).ok is False


# --- MCP leg TLS guard (returns before any I/O) -----------------------------


@pytest.mark.unit
@pytest.mark.parametrize("url", ["http://app.example.test", "app.example.test", "ws://x"])
def test_check_mcp_refuses_non_https(url: str) -> None:
    # The https:// guard returns a failing Result before importing/connecting anything,
    # so this never touches the network — the OAuth bearer is never sent over a non-TLS URL.
    result = smoke._check_mcp(url, "q")
    assert result.ok is False
    assert "https://" in result.detail
