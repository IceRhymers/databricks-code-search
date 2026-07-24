"""Shared error types for the serve-side search layer.

Kept in its own module so :mod:`app.search.symbols`, :mod:`app.search.grep`, and
:mod:`app.search.references` all raise the SAME error types without one importing another
(a timeout or an invalid regex in any of them must be indistinguishable to the MCP layer).
:func:`reraise_or_recoverable` is the single mapper from a Postgres fault -- surfaced by
SQLAlchemy as a :class:`~sqlalchemy.exc.DBAPIError` -- to one of the typed errors below, or a
re-raise of the original error when it maps to neither.
"""

from __future__ import annotations

from typing import NoReturn

import psycopg
from sqlalchemy.exc import DBAPIError


class QueryTooBroadError(Exception):
    """The per-request statement_timeout cancelled a query (candidate/content/symbol)."""


class RegexInvalidError(Exception):
    """A user-supplied pattern is not a valid Postgres POSIX ARE (regardless of polarity).

    Raised for a ``Regex`` atom, a ``repo:``/``file:`` filter, or a ``sym:`` filter whose
    pattern Postgres rejects with ``InvalidRegularExpression`` (SQLSTATE class 22, a Data
    Exception) -- e.g. ``/[/``. The message is ``str(error.orig)``, which describes only the
    caller's own pattern (no host/schema/relation leakage), so it is safe to surface verbatim
    to the caller, mirroring how ``query_parse_error`` echoes a parser message.
    """


def reraise_or_recoverable(error: DBAPIError) -> NoReturn:
    """Map a Postgres fault to a typed recoverable error, or re-raise it unchanged.

    ``psycopg.errors.QueryCanceled`` (a statement_timeout cancellation) maps to
    :class:`QueryTooBroadError`; ``psycopg.errors.InvalidRegularExpression`` (a Postgres-invalid
    POSIX ARE, e.g. ``/[/``) maps to :class:`RegexInvalidError`. Both classes are reachable
    through :class:`~sqlalchemy.exc.DBAPIError`'s two sibling subclasses --
    :class:`~sqlalchemy.exc.OperationalError` for the cancellation,
    :class:`~sqlalchemy.exc.DataError` for the invalid regex -- so every raw-execution call
    site catches the common ``DBAPIError`` ancestor and routes through this single mapper.
    Anything else (a NUL-byte ``DataError``, an ``IntegrityError``, ...) is re-raised
    unchanged: this widening is behavior-neutral for every error class it does not name.
    """
    if isinstance(error.orig, psycopg.errors.QueryCanceled):
        raise QueryTooBroadError(
            "the per-request statement_timeout cancelled a query (candidate, content, or "
            "symbol fetch) -- the query is too broad for the time budget"
        ) from error
    if isinstance(error.orig, psycopg.errors.InvalidRegularExpression):
        raise RegexInvalidError(str(error.orig)) from error
    raise error
