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

SENTINEL = "ghp_SENTINEL_tok_do_not_log_0xDEADBEEF"

_INDEXER_DIR = Path(__file__).resolve().parents[2] / "indexer"


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
    if len(parts) == 3:
        return httpx.Response(200, json={"default_branch": "main"})
    if parts[3] == "commits":
        return httpx.Response(200, json={"sha": "sha_widgets"})
    if parts[3] == "tarball":
        return httpx.Response(200, content=_tarball())
    return httpx.Response(404)


class _FakeConn:
    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeEngine:
    def connect(self) -> _FakeConn:
        return _FakeConn()

    def dispose(self) -> None:
        pass


def _index_fn(
    conn: Any,
    *,
    name: str,
    default_branch: Any,
    head_sha: str,
    items: Any,
    chunk_writer: Any = None,
) -> IndexCounts:
    files = len(list(items))
    return IndexCounts(files=files, symbols=0, swept=0)


@pytest.mark.unit
def test_token_never_appears_in_debug_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)  # root logger forced to DEBUG
    wc = _FakeWorkspaceClient()

    token = read_github_token(wc, "scope", "key")
    assert token == SENTINEL  # sanity: the sentinel really is the decoded token

    with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
        code = run(
            repos="acme/widgets",
            scope="scope",
            key="key",
            endpoint="ep",
            database="db",
            workspace_client=wc,
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_index_fn,
        )
    assert code == 0

    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()


def _error_handler(request: httpx.Request) -> httpx.Response:
    # Fail the tarball download so run() hits its per-repo `logger.exception` path
    # (an httpx error carries the request, whose headers hold the Authorization token).
    parts = request.url.path.strip("/").split("/")
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
            repos="acme/widgets",
            scope="scope",
            key="key",
            endpoint="ep",
            database="db",
            workspace_client=wc,
            http_client=client,
            engine=_FakeEngine(),
            index_fn=_index_fn,
        )
    assert code == 1  # the repo failed and was isolated
    assert SENTINEL not in caplog.text
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()


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
