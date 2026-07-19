"""Engine factory for the code search database.

One and only one place knows how to connect to Lakebase. The sole Lakebase API
deviation (Autoscaling ``w.postgres.generate_database_credential``) is confined
to this module, so a future fallback to the Provisioned ``w.database.*`` API is a
one-file change.

Dual-mode selection: a configured Lakebase endpoint wins over ``PGHOST``.

* Lakebase endpoint set (``endpoint=`` / ``LAKEBASE_ENDPOINT``) -> Lakebase mode, even if
  ``PGHOST`` is also present. The deployed app's Lakebase ``postgres`` binding injects
  ``PGHOST``/``PGUSER``/... at runtime, but those are not paired with a usable password, so
  the endpoint's presence — not the absence of ``PGHOST`` — is what selects Lakebase.
* ``PGHOST`` set and NO endpoint -> plain local Postgres (CI, ``migrate-local``, unit tests).
  The Databricks SDK is never imported on this path.
* Lakebase mode: a single ``WorkspaceClient`` is closed over
  by a ``do_connect`` event handler that injects a fresh OAuth token as the
  connection password on every physical connect (tokens live ~1h; the pool is
  recycled well under that).

The OAuth token is never logged.
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import URL, event
from sqlalchemy.engine import Engine, create_engine

_DEFAULT_PORT = 5432
# Small pool: the indexer and MCP server are low-concurrency workloads.
_POOL_SIZE = 5
# 45 min, comfortably under the ~1h Lakebase OAuth token TTL.
_POOL_RECYCLE = 2700


def _local_url() -> URL:
    """Build the local Postgres URL from the standard PG* environment variables."""
    port = os.environ.get("PGPORT")
    return URL.create(
        "postgresql+psycopg",
        username=os.environ.get("PGUSER"),
        password=os.environ.get("PGPASSWORD"),
        host=os.environ["PGHOST"],
        port=int(port) if port else _DEFAULT_PORT,
        database=os.environ.get("PGDATABASE"),
    )


def _lakebase_url(*, host: str, database: str | None, user: str | None) -> URL:
    """Build the Lakebase URL. The password is injected per-connect, not here."""
    port = os.environ.get("LAKEBASE_PORT")
    return URL.create(
        "postgresql+psycopg",
        username=user,
        host=host,
        port=int(port) if port else _DEFAULT_PORT,
        database=database,
        query={"sslmode": "require"},
    )


def create_db_engine(
    *,
    endpoint: str | None = None,
    host: str | None = None,
    database: str | None = None,
    user: str | None = None,
    echo: bool = False,
    **pool_kwargs: Any,
) -> Engine:
    """Create a SQLAlchemy :class:`~sqlalchemy.engine.Engine` for the code search DB.

    When ``PGHOST`` is set, a plain local Postgres engine is returned and the
    Databricks SDK is never touched. Otherwise a Lakebase engine is returned that
    refreshes its OAuth credential on every physical connection.

    Keyword args (Lakebase mode) fall back to environment variables when omitted:
    ``endpoint`` -> ``LAKEBASE_ENDPOINT``, ``host`` -> ``LAKEBASE_HOST``,
    ``database`` -> ``LAKEBASE_DATABASE``, ``user`` -> ``LAKEBASE_USER``.

    ``endpoint`` is the Lakebase endpoint identifier as expected by the Lakebase
    Autoscaling API (a name or resource path); it is passed through to the SDK as-is.
    """
    # PGHOST alone means plain local Postgres (CI, migrate-local, unit tests) — but a deployed
    # Databricks App with the Lakebase `postgres` resource binding ALSO gets PGHOST/PGUSER/...
    # injected, so PGHOST is no longer sufficient to distinguish local from Lakebase. When a
    # Lakebase endpoint is configured (endpoint arg or LAKEBASE_ENDPOINT), take the Lakebase
    # OAuth path even if the binding injected PGHOST: that injected PGHOST is NOT paired with a
    # usable password (Lakebase needs a freshly minted OAuth token as the password, not PGPASSWORD).
    lakebase_configured = bool(endpoint or os.environ.get("LAKEBASE_ENDPOINT"))
    if os.environ.get("PGHOST") and not lakebase_configured:
        return create_engine(_local_url(), echo=echo, **pool_kwargs)
    return _create_lakebase_engine(
        endpoint=endpoint,
        host=host,
        database=database,
        user=user,
        echo=echo,
        **pool_kwargs,
    )


def _create_lakebase_engine(
    *,
    endpoint: str | None,
    host: str | None,
    database: str | None,
    user: str | None,
    echo: bool,
    **pool_kwargs: Any,
) -> Engine:
    # Lazy import so the local/unit path never imports the SDK network layer.
    from databricks.sdk import WorkspaceClient

    endpoint_name = endpoint or os.environ.get("LAKEBASE_ENDPOINT")
    if not endpoint_name:
        raise ValueError(
            "A Lakebase endpoint identifier is required (a name or resource path as "
            "expected by the Lakebase Autoscaling API): pass endpoint=... or set "
            "LAKEBASE_ENDPOINT."
        )

    # One WorkspaceClient, closed over by the do_connect handler below.
    wc = WorkspaceClient()

    # Pre-flight: on older SDKs this method lives only on w.database.
    if not hasattr(wc.postgres, "generate_database_credential"):
        raise RuntimeError(
            "WorkspaceClient().postgres.generate_database_credential is unavailable; "
            "the Lakebase Autoscaling credential API requires databricks-sdk>=0.81.0."
        )

    database_name = database or os.environ.get("LAKEBASE_DATABASE")
    user_name = user or os.environ.get("LAKEBASE_USER")
    if not user_name:
        user_name = wc.current_user.me().user_name

    resolved_host = host or os.environ.get("LAKEBASE_HOST")
    if not resolved_host:
        # Keep the endpoint NAME distinct from the DNS host.
        status = wc.postgres.get_endpoint(endpoint_name).status
        hosts = status.hosts if status else None
        resolved_host = hosts.host if hosts else None
        if not resolved_host:
            raise RuntimeError(
                f"Could not resolve a DNS host for Lakebase endpoint {endpoint_name!r}; "
                "pass host=... or set LAKEBASE_HOST."
            )

    url = _lakebase_url(host=resolved_host, database=database_name, user=user_name)

    pool_kwargs.setdefault("pool_size", _POOL_SIZE)
    pool_kwargs.setdefault("pool_pre_ping", True)
    pool_kwargs.setdefault("pool_recycle", _POOL_RECYCLE)

    engine = create_engine(url, echo=echo, **pool_kwargs)

    @event.listens_for(engine, "do_connect")
    def _inject_oauth_token(
        _dialect: Any, _conn_rec: Any, _cargs: Any, cparams: dict[str, Any]
    ) -> None:
        # Fresh token per physical connection. Never logged.
        cparams["password"] = wc.postgres.generate_database_credential(endpoint=endpoint_name).token

    return engine
