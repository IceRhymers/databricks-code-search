"""Post-deploy smoke test for the code-search MCP app (issue #12).

Proves a *real* result after ``make deploy``, never a false green. Each probe has an
explicit meaning (see the grant-oracle table in ``.omc/plans/issue-12-deploy-plan.md``):

* ``GET /health`` == 200  -> liveness (ALWAYS).
* ``GET /ready``  == 200  -> the app SP's SELECT grant landed; this is the always-on
  **grant oracle** (it runs as the app SP against a protected table, 0-row-safe). Direct-SQL
  as the owner cannot detect a missing app grant, so ``/ready`` — not direct-SQL — is the
  honest oracle.
* direct-SQL ``SELECT 1`` as the developer/owner -> connectivity only, NOT a grant assertion.
* direct-SQL ``SELECT count(*) FROM repos >= 1`` -> corpus populated; only under
  ``--expect-indexed`` (a fresh deploy has an empty corpus, so this is opt-in and must not
  false-RED).
* MCP ``search_code`` -> end-to-end query returns file-grouped matches with line numbers; only
  under ``--enable-mcp`` (opt-in, since it needs a real query term against a populated corpus).

Databricks Apps sit behind Databricks OAuth: an unauthenticated request is 302-redirected to
the login page. Every request to the app (``/health``, ``/ready``, and the MCP leg) therefore
carries a fresh OAuth bearer from ``WorkspaceClient().config.authenticate()`` (the uc-mcp-proxy
pattern), which transparently works for U2M (``databricks auth login``) or M2M (a service
principal via ``DATABRICKS_CLIENT_ID``/``DATABRICKS_CLIENT_SECRET`` with ``CAN_USE`` on the app).
No M2M is required just to reach the endpoint — your interactive login suffices.

The pure predicate functions (``health_ok`` .. ``validate_search_payload``) do no I/O and carry
no heavy imports, so they are unit-testable without a live app; the live legs import ``httpx`` /
``mcp`` / ``sqlalchemy`` / ``databricks.sdk`` lazily inside their functions.

Exit contract: non-zero if ``/health`` or ``/ready`` fail, if connectivity fails, if
``--expect-indexed`` and the corpus is empty, or if ``--enable-mcp`` is set and the MCP leg
fails. Zero (with an ``mcp: SKIPPED`` note) when ``--enable-mcp`` is not passed. Never a silent
green.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, NamedTuple


class Result(NamedTuple):
    """A single leg's outcome: ``ok`` gates the exit code, ``detail`` is human-facing."""

    ok: bool
    detail: str


# --------------------------------------------------------------------- pure predicates
#
# No I/O, stdlib-only imports: importable and unit-testable without a live app.


def health_ok(body: dict[str, Any]) -> bool:
    """Liveness predicate: the ``/health`` body reports ``status == "ok"``."""
    return body.get("status") == "ok"


def ready_ok(status_code: int) -> bool:
    """Grant-oracle predicate: ``/ready`` returned 200 (the app SP SELECT grant landed)."""
    return status_code == 200


def assert_connectivity(scalar: Any) -> Result:
    """A returned ``SELECT 1`` scalar proves connectivity — explicitly NOT a grant assertion."""
    ok = scalar == 1
    if ok:
        return Result(True, "connectivity: SELECT 1 returned 1 (owner; not a grant check)")
    return Result(False, f"connectivity: SELECT 1 returned {scalar!r}, expected 1")


def assert_corpus_nonempty(count: int) -> Result:
    """Corpus predicate (only under ``--expect-indexed``): ``count >= 1``."""
    ok = count >= 1
    if ok:
        return Result(True, f"corpus: repos count = {count} (>= 1)")
    return Result(False, "corpus: repos count = 0 (expected >= 1 under --expect-indexed)")


def validate_search_payload(payload: dict[str, Any]) -> Result:
    """Assert the pinned ``search_code`` envelope: ``file_count > 0``, each file has ``matches``,
    each match carries an integer ``line`` and a ``byte_ranges`` list (zoekt parity shape)."""
    file_count = payload.get("file_count")
    if not isinstance(file_count, int) or file_count <= 0:
        return Result(False, f"search_code: file_count = {file_count!r}, expected a positive int")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return Result(False, "search_code: 'files' is empty or not a list")
    for fi, f in enumerate(files):
        matches = f.get("matches") if isinstance(f, dict) else None
        if not isinstance(matches, list) or not matches:
            return Result(False, f"search_code: files[{fi}].matches is empty or not a list")
        for mi, m in enumerate(matches):
            if not isinstance(m, dict) or not isinstance(m.get("line"), int):
                return Result(False, f"search_code: files[{fi}].matches[{mi}].line is not an int")
            if not isinstance(m.get("byte_ranges"), list):
                return Result(
                    False, f"search_code: files[{fi}].matches[{mi}].byte_ranges is not a list"
                )
    return Result(True, f"search_code: {file_count} file(s) with well-formed matches")


# ------------------------------------------------------------------------------ live legs
#
# Heavy imports (httpx / mcp / sqlalchemy / databricks.sdk) are lazy so the pure predicates
# above stay import-light and unit-testable without a live deployment.


def _auth_headers() -> dict[str, str]:
    """Databricks OAuth headers for the CURRENT identity (uc-mcp-proxy pattern).

    ``WorkspaceClient().config.authenticate()`` returns ``{"Authorization": "Bearer <token>"}``
    for whatever the SDK resolves — U2M (``databricks auth login``) or M2M (an SP via
    ``DATABRICKS_CLIENT_ID``/``DATABRICKS_CLIENT_SECRET``). The app front door 302-redirects any
    request without this. We also forward ``X-Forwarded-Access-Token`` so the app could use the
    caller's identity (our shared-SP app ignores it; harmless to send). A fresh call per smoke
    run keeps the ~1h token current.
    """
    from databricks.sdk import WorkspaceClient

    headers = dict(WorkspaceClient().config.authenticate())
    token = headers.get("Authorization", "")
    if token.startswith("Bearer "):
        headers["X-Forwarded-Access-Token"] = token[len("Bearer ") :]
    return headers


def _http_get_json(url: str, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    """GET ``url`` with OAuth headers; return ``(status_code, json_body)`` (non-JSON -> ``{}``).

    ``follow_redirects`` stays OFF so an auth failure surfaces as a clear ``302`` rather than a
    silent 200 of the login page's HTML.
    """
    import httpx

    resp = httpx.get(url, headers=headers, timeout=15.0)
    try:
        body = resp.json()
    except Exception:
        body = {}
    return resp.status_code, body


def _check_health(app_url: str, headers: dict[str, str]) -> Result:
    status, body = _http_get_json(f"{app_url}/health", headers)
    if status == 200 and health_ok(body):
        return Result(True, "GET /health = 200 status:ok")
    hint = " (302 = OAuth redirect: not authenticated)" if status == 302 else ""
    return Result(False, f"GET /health = {status}{hint} body={body!r}")


def _check_ready(app_url: str, headers: dict[str, str]) -> Result:
    status, _ = _http_get_json(f"{app_url}/ready", headers)
    if ready_ok(status):
        return Result(True, "GET /ready = 200 (app SP SELECT grant landed)")
    hint = " (302 = OAuth redirect: not authenticated)" if status == 302 else ""
    return Result(
        False, f"GET /ready = {status}{hint} (grant oracle: app SP cannot SELECT, or DB down)"
    )


def _check_db(expect_indexed: bool) -> list[Result]:
    """Direct-SQL legs as the developer/owner: connectivity always; corpus under the flag.

    The corpus query assumes the default schema (``repos`` on the connection's ``search_path``),
    which is what ``make deploy`` uses (migrate resolves ``current_schema()`` when ``PGSCHEMA`` is
    unset). ``/ready`` — not this leg — is the authoritative grant oracle; this is owner-side
    connectivity plus an optional corpus check.
    """
    from sqlalchemy import text

    from app.db.client import create_db_engine

    results: list[Result] = []
    engine = create_db_engine()
    try:
        with engine.connect() as conn:
            scalar = conn.execute(text("SELECT 1")).scalar()
            results.append(assert_connectivity(scalar))
            if expect_indexed:
                count = conn.execute(text("SELECT count(*) FROM repos")).scalar()
                results.append(assert_corpus_nonempty(int(count or 0)))
    finally:
        engine.dispose()
    return results


def _check_mcp(app_url: str, query: str, headers: dict[str, str] | None = None) -> Result:
    """Live MCP leg: call ``search_code`` over authenticated streamable HTTP, validate envelope."""
    import asyncio
    import json

    # This leg sends a real Databricks OAuth bearer to app_url; refuse a non-TLS URL so the
    # token can never be emitted in cleartext (or to an attacker-supplied http:// host). Checked
    # before any auth/network so a bad URL fails fast.
    if not app_url.startswith("https://"):
        return Result(
            False,
            f"MCP leg refused: --app-url must be https:// to carry the OAuth bearer "
            f"(got {app_url!r})",
        )
    auth_headers = headers if headers is not None else _auth_headers()

    async def _run() -> Result:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(f"{app_url}/mcp", headers=auth_headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                res = await session.call_tool("search_code", {"query": query})
                text_parts = [c.text for c in res.content if getattr(c, "type", None) == "text"]
                payload = json.loads(text_parts[0]) if text_parts else {}
                return validate_search_payload(payload)

    return asyncio.run(_run())


# ------------------------------------------------------------------------------------ main


def _print_leg(name: str, status: str, detail: str) -> None:
    print(f"[{status:>7}] {name}: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post-deploy smoke test for the MCP app.")
    parser.add_argument("--app-url", required=True, help="Base URL of the deployed app.")
    parser.add_argument(
        "--expect-indexed",
        action="store_true",
        help="Also assert the corpus is non-empty (SELECT count(*) FROM repos >= 1).",
    )
    parser.add_argument(
        "--enable-mcp",
        action="store_true",
        help="Also exercise the live MCP search_code leg (uses your Databricks login; needs a "
        "populated corpus so the query returns matches).",
    )
    parser.add_argument(
        "--query",
        default="the",
        help="Query for the MCP search_code leg (default: a common term).",
    )
    args = parser.parse_args(argv)

    app_url = args.app_url.rstrip("/")
    failed = False

    def _guard(name: str, fn: Any) -> Result:
        """Turn a live leg's exception into a gating FAIL line, not a raw traceback."""
        try:
            return fn()
        except Exception as exc:  # unreachable app / engine build failure / no Databricks auth
            return Result(False, f"{name}: raised {type(exc).__name__}: {exc}")

    # Databricks OAuth headers for the app front door, minted once. If no usable identity is
    # configured, _auth_headers raises → /health and /ready then FAIL (not a silent 302).
    try:
        headers = _auth_headers()
    except Exception as exc:
        headers = {}
        _print_leg("auth", "FAIL", f"could not mint a Databricks OAuth token: {exc}")
        failed = True

    # /health and /ready — always gating.
    health = _guard("health", lambda: _check_health(app_url, headers))
    ready = _guard("ready", lambda: _check_ready(app_url, headers))
    for name, result in (("health", health), ("ready", ready)):
        _print_leg(name, "PASS" if result.ok else "FAIL", result.detail)
        failed = failed or not result.ok

    # Direct-SQL: connectivity always; corpus only under --expect-indexed. Both gating. A raised
    # engine/connection error becomes a single gating FAIL line rather than crashing the run.
    try:
        db_results = _check_db(args.expect_indexed)
    except Exception as exc:
        db_results = [Result(False, f"raised {type(exc).__name__}: {exc}")]
    for result in db_results:
        _print_leg("direct-sql", "PASS" if result.ok else "FAIL", result.detail)
        failed = failed or not result.ok

    # MCP: opt-in via --enable-mcp (needs a real query term against a populated corpus).
    # Authenticated with the same Databricks identity — no M2M required.
    if args.enable_mcp:
        result = _guard("mcp", lambda: _check_mcp(app_url, args.query, headers))
        _print_leg("mcp", "PASS" if result.ok else "FAIL", result.detail)
        failed = failed or not result.ok
    else:
        _print_leg(
            "mcp", "SKIPPED", "pass --enable-mcp to exercise search_code with your Databricks login"
        )

    print("smoke: FAIL" if failed else "smoke: PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
