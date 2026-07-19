"""Shared error types for the serve-side search layer.

Kept in its own module so :mod:`app.search.symbols` and :mod:`app.search.grep` both raise
the SAME :class:`QueryTooBroadError` without one importing the other (a timeout in either
serve path must be indistinguishable to the MCP layer). :func:`reraise_or_query_too_broad`
is the single mapper from a Postgres ``statement_timeout`` cancellation to that error.
"""

from __future__ import annotations

from typing import NoReturn

import psycopg
from sqlalchemy.exc import OperationalError


class QueryTooBroadError(Exception):
    """The per-request statement_timeout cancelled a query (candidate/content/symbol)."""


def reraise_or_query_too_broad(error: OperationalError) -> NoReturn:
    """Map a Postgres statement_timeout cancellation to :class:`QueryTooBroadError`.

    Any other :class:`~sqlalchemy.exc.OperationalError` (e.g. an invalid POSIX regex bound
    into a ``sym:``/content pattern) is re-raised unchanged.
    """
    if isinstance(error.orig, psycopg.errors.QueryCanceled):
        raise QueryTooBroadError(
            "the per-request statement_timeout cancelled a query (candidate, content, or "
            "symbol fetch) -- the query is too broad for the time budget"
        ) from error
    raise error
