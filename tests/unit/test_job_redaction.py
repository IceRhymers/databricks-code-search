"""Redaction proof + source-level tripwire for the GitHub token.

Two independent guards:

1. *Behavioral* — drive ``read_github_token`` plus a fully-faked repo pipeline with
   the root logger forced to DEBUG, and assert the sentinel token never appears in
   any captured log output. Proof for the exercised path.
2. *Source-level tripwire* (necessary, not sufficient) — grep ``indexer/`` and assert
   no DEBUG level-lowering / SDK-logger level-raising. This is a regression guard for
   the real SDK network path that a creds-less fake cannot exercise (CLI #4980).

Deliberately does NOT assert ``getLogger().level != DEBUG`` at runtime: caplog forces
root to DEBUG (contradictory) and basicConfig is a no-op under pytest's handler.
"""

from __future__ import annotations

import base64
import io
import logging
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from indexer.job import read_github_token, run
from indexer.languages import IndexCounts
from indexer.repo_config import RepoConfig
from indexer.store import ReconcileCounts

SENTINEL = "ghp_SENTINEL_tok_do_not_log_0xDEADBEEF"

_INDEXER_DIR = Path(__file__).resolve().parents[2] / "indexer"

# Explicit repos: the pipeline tests below assert per-repo indexing, which needs a
# deterministic single repo. The enumeration path gets its own config (AC 41).
_CONFIG = RepoConfig.model_validate(
    {"version": 1, "connections": [{"type": "github", "repos": ["acme/widgets"]}]}
)
_ORG_CONFIG = RepoConfig.model_validate(
    {"version": 1, "connections": [{"type": "github", "orgs": ["acme"]}]}
)


class _FakeSecret:
    value = base64.b64encode(SENTINEL.encode()).decode()


class _FakeSecrets:
    def get_secret(self, scope: str, key: str) -> _FakeSecret:
        return _FakeSecret()


class _FakeWorkspaceClient:
    secrets = _FakeSecrets()


def _tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"def f():\n    return 1\n"
        info = tarfile.TarInfo("acme-widgets-shashas/main.py")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _handler(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.strip("/").split("/")
    # Enumeration. MUST precede the len(parts) == 3 arm below: /orgs/acme/repos
    # also has three segments and would otherwise be answered with repo metadata.
    if len(parts) == 3 and parts[0] in {"orgs", "users"} and parts[2] == "repos":
        return httpx.Response(
            200,
            json=[{"full_name": "acme/widgets", "fork": False, "archived": False, "size": 1}],
        )
    if len(parts) == 3:
        return httpx.Response(200, json={"default_branch": "main"})
    if parts[3] == "commits":
        return httpx.Response(200, json={"sha": "sha_widgets"})
    if parts[3] == "tarball":
        return httpx.Response(200, content=_tarball())
    return httpx.Response(404)


class _FakeResult:
    def all(self) -> list[Any]:
        return []

    def scalars(self) -> _FakeResult:
        """Answers the reconciliation checkpoint's ``select(Repo.name)`` (stored repos)."""
        return self


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def rollback(self) -> None:
        """No-op: real code clears the purge-guard read's autobegun txn here."""

    def execute(self, stmt: Any) -> _FakeResult:
        """Answers run()'s pre-fan-out stamp SELECT. No rows = nothing skipped,
        which keeps every test here on the full fetch->index path."""
        return _FakeResult()


class _FakeEngine:
    def connect(self) -> _FakeConn:
        return _FakeConn()

    def dispose(self) -> None:
        pass


def _index_fn(
    conn: Any,
    *,
    name: str,
    branch: str,
    is_default: bool,
    head_sha: str,
    items: Any,
    chunk_writer: Any = None,
) -> IndexCounts:
    files = len(list(items))
    return IndexCounts(files=files, symbols=0, swept=0)


# No-op reconcile fns: a clean run in these tests always passes
# _decide_reconciliation's gate, so run() reaches the post-fan-out checkpoint and
# would otherwise call the REAL indexer.store primitives against these fake
# conn/engine objects (neither of which implements conn.begin()). Every clean-run
# test below injects these explicitly rather than relying on the defaults.
def _reconcile_retired_noop(conn: Any, *, name: str, retired_branches: Any) -> ReconcileCounts:
    return ReconcileCounts(branches_removed=0, files_stripped=0, files_deleted=0)


def _reconcile_removed_noop(conn: Any, *, desired_repos: Any) -> list[str]:
    return []


@pytest.mark.unit
def test_token_never_appears_in_debug_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)  # root logger forced to DEBUG
    wc = _FakeWorkspaceClient()

    token = read_github_token(wc, "scope", "key")
    assert token == SENTINEL  # sanity: the sentinel really is the decoded token

    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        code = run(
            config_path="/Workspace/x/config.yaml",
            scope="scope",
            key="key",
            endpoint="ep",
            database="db",
            workspace_client=wc,
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_index_fn,
            config_loader=lambda _c, _p: _CONFIG,
            reconcile_retired_fn=_reconcile_retired_noop,
            reconcile_removed_fn=_reconcile_removed_noop,
        )
    assert code == 0

    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()


def _error_handler(request: httpx.Request) -> httpx.Response:
    # Fail the tarball download so run() hits its per-repo `logger.exception` path
    # (an httpx error carries the request, whose headers hold the Authorization token).
    parts = request.url.path.strip("/").split("/")
    # Enumeration. MUST precede the len(parts) == 3 arm below: /orgs/acme/repos
    # also has three segments and would otherwise be answered with repo metadata.
    if len(parts) == 3 and parts[0] in {"orgs", "users"} and parts[2] == "repos":
        return httpx.Response(
            200,
            json=[{"full_name": "acme/widgets", "fork": False, "archived": False, "size": 1}],
        )
    if len(parts) == 3:
        return httpx.Response(200, json={"default_branch": "main"})
    if parts[3] == "commits":
        return httpx.Response(200, json={"sha": "sha_widgets"})
    return httpx.Response(500)


@pytest.mark.unit
def test_token_never_appears_on_error_path(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    wc = _FakeWorkspaceClient()
    with httpx.Client(transport=httpx.MockTransport(_error_handler)) as client:
        code = run(
            config_path="/Workspace/x/config.yaml",
            scope="scope",
            key="key",
            endpoint="ep",
            database="db",
            workspace_client=wc,
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_index_fn,
            config_loader=lambda _c, _p: _CONFIG,
        )
    assert code == 1  # the repo failed and was isolated
    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()


@pytest.mark.unit
def test_token_never_appears_during_enumeration(caplog: pytest.LogCaptureFixture) -> None:
    """AC 41: enumeration is a new HTTP path, and it carries the same header.

    ``indexer.fetch`` logs ``X-RateLimit-Remaining`` per selector; a careless
    edit that logged the whole request/headers instead would leak the token.
    ``record.args`` is checked too — lazy %-formatting means a token passed as an
    argument never reaches ``getMessage()`` unless the record is rendered.
    """
    caplog.set_level(logging.DEBUG)
    wc = _FakeWorkspaceClient()
    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        code = run(
            config_path="/Workspace/x/config.yaml",
            scope="scope",
            key="key",
            endpoint="ep",
            database="db",
            workspace_client=wc,
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_index_fn,
            config_loader=lambda _c, _p: _ORG_CONFIG,
            reconcile_retired_fn=_reconcile_retired_noop,
            reconcile_removed_fn=_reconcile_removed_noop,
        )
    assert code == 0  # the org enumerated, and its one repo indexed

    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        for arg in record.args or ():
            assert SENTINEL not in str(arg)


@pytest.mark.unit
def test_reconciliation_failure_never_logs_exception_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reconcile primitive raising must never leak its exception message.

    The purge decision (``select(Repo.name)``) always runs once the gate passes
    regardless of what the fakes return, so ``reconcile_removed_fn`` raising here
    reliably exercises ``_reconcile``'s except branch -- unlike
    ``reconcile_retired_fn``, which this fake setup's empty stamp snapshot would
    never even call (nothing is ever "retired" against an empty persisted set).
    """
    caplog.set_level(logging.DEBUG)
    wc = _FakeWorkspaceClient()

    def _reconcile_removed_leaks(conn: Any, *, desired_repos: Any) -> list[str]:
        raise RuntimeError(f"db error near table: {SENTINEL}")

    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        code = run(
            config_path="/Workspace/x/config.yaml",
            scope="scope",
            key="key",
            endpoint="ep",
            database="db",
            workspace_client=wc,
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_index_fn,
            config_loader=lambda _c, _p: _CONFIG,
            reconcile_retired_fn=_reconcile_retired_noop,
            reconcile_removed_fn=_reconcile_removed_leaks,
        )
    assert code == 1  # reconciliation failed -> non-zero exit

    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        for arg in record.args or ():
            assert SENTINEL not in str(arg)


@pytest.mark.unit
def test_source_has_no_debug_level_lowering() -> None:
    forbidden = (
        "basicConfig(level=logging.DEBUG",
        "setLevel(logging.DEBUG)",
        "setLevel(DEBUG)",
        'setLevel("DEBUG")',
    )
    for path in _INDEXER_DIR.glob("*.py"):
        src = path.read_text()
        for needle in forbidden:
            assert needle not in src, f"{path.name} lowers logging to DEBUG: {needle!r}"


@pytest.mark.unit
def test_source_does_not_touch_sdk_or_http_logger_levels() -> None:
    for path in _INDEXER_DIR.glob("*.py"):
        src = path.read_text()
        for noisy in ("databricks", "httpx", "urllib3"):
            assert f'getLogger("{noisy}")' not in src, (
                f"{path.name} manipulates the {noisy} logger near the token path"
            )
