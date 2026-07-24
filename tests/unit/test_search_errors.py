"""Unit tests for ``app.search.errors``: the shared Postgres-fault -> typed-error mapper.

No DB: ``reraise_or_recoverable`` is exercised with real ``psycopg.errors`` instances wrapped
in the SQLAlchemy ``DBAPIError`` subclass a raw execution site would actually catch them as
(``OperationalError`` for a cancellation, ``DataError`` for an invalid regex or a NUL byte).
"""

from __future__ import annotations

import psycopg
import pytest
from sqlalchemy.exc import DataError, IntegrityError, OperationalError

from app.search.errors import QueryTooBroadError, RegexInvalidError, reraise_or_recoverable


def _wrap(cls: type[Exception], orig: Exception) -> Exception:
    return cls("SELECT 1", {}, orig)


@pytest.mark.unit
def test_query_canceled_maps_to_query_too_broad_error() -> None:
    orig = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    error = _wrap(OperationalError, orig)

    with pytest.raises(QueryTooBroadError):
        reraise_or_recoverable(error)  # type: ignore[arg-type]


@pytest.mark.unit
def test_invalid_regular_expression_maps_to_regex_invalid_error_carrying_message() -> None:
    orig = psycopg.errors.InvalidRegularExpression(
        "invalid regular expression: brackets [] not balanced"
    )
    error = _wrap(DataError, orig)

    with pytest.raises(RegexInvalidError, match=r"brackets \[\] not balanced") as excinfo:
        reraise_or_recoverable(error)  # type: ignore[arg-type]
    assert str(excinfo.value) == "invalid regular expression: brackets [] not balanced"


@pytest.mark.unit
def test_generic_data_error_is_reraised_unchanged() -> None:
    # NUL-byte shape (see tests/unit/test_webui_main.py): a DataError this mapper does not
    # name must propagate unchanged, never silently swallowed or reclassified.
    orig = ValueError("PostgreSQL text fields cannot contain NUL (0x00) bytes")
    error = _wrap(DataError, orig)

    with pytest.raises(DataError) as excinfo:
        reraise_or_recoverable(error)  # type: ignore[arg-type]
    assert excinfo.value is error


@pytest.mark.unit
def test_generic_operational_error_is_reraised_unchanged() -> None:
    orig = psycopg.errors.OperationalError("connection reset")
    error = _wrap(OperationalError, orig)

    with pytest.raises(OperationalError) as excinfo:
        reraise_or_recoverable(error)  # type: ignore[arg-type]
    assert excinfo.value is error


@pytest.mark.unit
def test_integrity_error_is_reraised_unchanged() -> None:
    # Breadth guard on the DBAPIError widening (OperationalError + DataError -> DBAPIError):
    # a sibling DBAPIError subclass this mapper does not name must also pass through untouched.
    orig = psycopg.errors.UniqueViolation("duplicate key value violates unique constraint")
    error = _wrap(IntegrityError, orig)

    with pytest.raises(IntegrityError) as excinfo:
        reraise_or_recoverable(error)  # type: ignore[arg-type]
    assert excinfo.value is error
