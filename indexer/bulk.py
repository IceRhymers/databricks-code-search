"""Param-budgeted multi-row ``INSERT`` helper shared by ``store.py`` and ``chunk_store.py``.

A separate module rather than a private helper in ``store.py``: ``chunk_store.py``
needs it too and deliberately does not import ``store.py`` (its own docstring says
it mirrors that module's connection seam, not depends on it). Not a
``SEMANTICS_PATHS`` file -- it changes how rows are written, never what is
extracted.

**Why an explicit multi-row ``.values([...])`` and not ``conn.execute(pg_insert(T),
[rows])``.** The executemany form is what the per-file code used before this
module existed, and SQLAlchemy would page it for us. But the round-trip
acceptance criterion this module exists to satisfy is stated in *statements*, and
executemany's round-trip count is a property of psycopg3's pipeline mode and
libpq's version -- an environment-dependent number. An explicit multi-row
``VALUES`` is unambiguously one statement on every driver and every target, and is
measurable identically in a unit fake, an integration test, and production.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Connection, Table
from sqlalchemy.dialects.postgresql import insert as pg_insert

# libpq's Bind message carries the parameter count in an int16, so no single
# statement may bind more than 65535 params. Callers size their own batches
# against this budget (see indexer/store.py's _BATCH_MAX_FILES *
# _FILE_UPSERT_COLUMNS < 65535 invariant); this is the generic slicing bound
# for the tables that are not directly param-count-limited by a batch cap.
PARAM_BUDGET = 30_000

# Payload-bounded, not param-bounded: each chunk row carries a 1024-float
# embedding vector, so ~1000 rows/statement keeps the wire payload around 4 MB
# instead of 20 MB.
CHUNK_PARAM_BUDGET = 6_000


def insert_rows(
    conn: Connection,
    table: Table,
    rows: Sequence[dict[str, Any]],
    *,
    param_budget: int = PARAM_BUDGET,
) -> None:
    """Issue one multi-row ``INSERT`` per ``param_budget``-sized slice of ``rows``.

    A no-op on an empty ``rows`` -- no statement is issued at all. Every row is
    present exactly once, in input order; no single statement binds more than
    ``param_budget`` params (``rows_per_statement = param_budget //
    len(rows[0])``, so this also bounds each statement's row count).
    """
    if not rows:
        return
    columns = len(rows[0])
    rows_per_statement = max(1, param_budget // columns)
    for start in range(0, len(rows), rows_per_statement):
        chunk = rows[start : start + rows_per_statement]
        conn.execute(pg_insert(table).values(list(chunk)))
