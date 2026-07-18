"""Integration tests for the query compiler (issue #9): AST -> executed SQL rows.

Requires a running Postgres with the standard PG* env set (schema idiom mirrors
``tests/integration/test_migrations.py``: a unique throwaway schema, ``SET
search_path``, ``Base.metadata.create_all`` -- which also builds the trgm GIN
indexes -- and ``DROP SCHEMA ... CASCADE`` cleanup in a ``finally``). In this repo
that Postgres only exists as CI's ``pgvector/pgvector:pg16`` service container
(``.github/workflows/ci.yml``); there is no local Postgres available here, so
these tests are CI-only and were validated by lint/type-check, not execution.

The ``seeded`` fixture is module-scoped: every test in this file only reads (plain
``SELECT`` executions and ``EXPLAIN``), so one seeded corpus serves the whole
module -- nothing here mutates rows, so sharing is safe and avoids rebuilding the
schema/corpus per test.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Connection, insert, text
from sqlalchemy.dialects import postgresql

from app.db.client import create_db_engine
from app.db.models import Base, File, Repo, Symbol
from app.query.compiler import compile_query
from app.query.parser import Node, parse, resolve_case

SCHEMA_PREFIX = "test_qcompiler"

# Paths seeded under each repo (see ``seeded`` fixture below) -- used to build
# expected result sets without re-deriving them per test.
ACME_PATHS = frozenset(
    {
        "src/handler.go",
        "src/util.go",
        "src/escape1.go",
        "src/escape2.go",
        "src/regex_lower.go",
        "SRC/UpperCase.go",
        "src/no_lang.txt",
    }
)
BETA_PATHS = frozenset({"pkg/regex_upper.py", "pkg/handler_note.py"})


class Seeded(NamedTuple):
    conn: Connection
    acme_id: int
    beta_id: int
    files: dict[str, int]  # path -> file id


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _insert_repo(conn: Connection, name: str) -> int:
    return conn.execute(insert(Repo).values(name=name).returning(Repo.id)).scalar_one()


def _insert_file(
    conn: Connection,
    repo_id: int,
    path: str,
    *,
    lang: str | None,
    content: str | None,
) -> int:
    return conn.execute(
        insert(File)
        .values(repo_id=repo_id, path=path, lang=lang, content=content)
        .returning(File.id)
    ).scalar_one()


def _insert_symbol(
    conn: Connection, file_id: int, repo_id: int, name: str, *, kind: str = "function"
) -> int:
    return conn.execute(
        insert(Symbol)
        .values(file_id=file_id, repo_id=repo_id, name=name, kind=kind)
        .returning(Symbol.id)
    ).scalar_one()


@pytest.fixture(scope="module")
def seeded() -> Iterator[Seeded]:
    """Throwaway schema + durable-core DDL + a small deterministic corpus.

    Mirrors ``test_migrations.py``'s throwaway-schema idiom (unique schema, ``SET
    search_path``, ``CREATE EXTENSION IF NOT EXISTS pg_trgm``, ``DROP SCHEMA ...
    CASCADE`` + ``engine.dispose()`` in ``finally``) and ``test_store.py``'s use of
    ``Base.metadata.create_all(bind=conn)`` on the *same* connection the search_path
    was set on (using the bare ``engine`` for DDL would build tables against the
    engine's default search_path, not this throwaway schema).
    """
    schema = _unique(SCHEMA_PREFIX)
    engine = create_db_engine()
    conn = engine.connect()
    try:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
        conn.execute(text(f"SET search_path TO {schema}, public"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.commit()

        Base.metadata.create_all(bind=conn)
        conn.commit()

        acme_id = _insert_repo(conn, "acme/widgets")
        beta_id = _insert_repo(conn, "beta/tools")

        files: dict[str, int] = {}
        files["src/handler.go"] = _insert_file(
            conn,
            acme_id,
            "src/handler.go",
            lang="go",
            content=(
                "package main\n\nfunc Handler() {}\n"
                "// Handler processes an incoming Request via parseRequest\n"
            ),
        )
        files["src/util.go"] = _insert_file(
            conn,
            acme_id,
            "src/util.go",
            lang="go",
            content=(
                "package main\n\nfunc helperFn() {}\n// generic handler utility, not exported\n"
            ),
        )
        files["src/escape1.go"] = _insert_file(
            conn, acme_id, "src/escape1.go", lang="go", content="token foo_bar here"
        )
        files["src/escape2.go"] = _insert_file(
            conn, acme_id, "src/escape2.go", lang="go", content="token fooXbar here"
        )
        files["src/regex_lower.go"] = _insert_file(
            conn, acme_id, "src/regex_lower.go", lang="go", content="alpha fooXXXbar beta"
        )
        files["SRC/UpperCase.go"] = _insert_file(
            conn,
            acme_id,
            "SRC/UpperCase.go",
            lang="go",
            content="case sensitivity path marker only",
        )
        files["src/no_lang.txt"] = _insert_file(
            conn,
            acme_id,
            "src/no_lang.txt",
            lang=None,
            content="handler mention with no lang set",
        )
        files["pkg/regex_upper.py"] = _insert_file(
            conn, beta_id, "pkg/regex_upper.py", lang="python", content="alpha FooXXXBar beta"
        )
        files["pkg/handler_note.py"] = _insert_file(
            conn,
            beta_id,
            "pkg/handler_note.py",
            lang="python",
            content="# handler mentioned here in beta too",
        )

        # Separate repo so this NULL-content/NULL-lang row perturbs no repo-scoped
        # exact-set assertion (ACME_PATHS / BETA_PATHS / lang:go); it exists only to
        # prove content predicates exclude a NULL-content row.
        gamma_id = _insert_repo(conn, "gamma/misc")
        files["gamma/null_content.txt"] = _insert_file(
            conn, gamma_id, "gamma/null_content.txt", lang=None, content=None
        )

        _insert_symbol(conn, files["src/handler.go"], acme_id, "Handler")
        _insert_symbol(conn, files["src/handler.go"], acme_id, "parseRequest")
        _insert_symbol(conn, files["src/util.go"], acme_id, "helperFn")

        conn.commit()

        yield Seeded(conn=conn, acme_id=acme_id, beta_id=beta_id, files=files)
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


def _rows(conn: Connection, node: Node, **kwargs: Any) -> list[tuple[int, str]]:
    """Execute the compiled query and return (repo_id, path) in result order."""
    stmt = compile_query(node, **kwargs)
    return [(row.repo_id, row.path) for row in conn.execute(stmt).all()]


def _paths(conn: Connection, query: str, **kwargs: Any) -> set[str]:
    """Parse + execute ``query`` and return the matched paths (order-independent)."""
    return {path for _repo_id, path in _rows(conn, parse(query), **kwargs)}


# --------------------------------------------------------------------------- substring


@pytest.mark.integration
def test_substring_case_insensitive_matches_regardless_of_letter_case(seeded: Seeded) -> None:
    assert _paths(seeded.conn, "handler") == {
        "src/handler.go",
        "src/util.go",
        "src/no_lang.txt",
        "pkg/handler_note.py",
    }


@pytest.mark.integration
def test_substring_case_sensitive_matches_only_exact_case(seeded: Seeded) -> None:
    # Same content family as above, but `case:yes Handler` must exclude every file
    # whose only occurrence is the lowercase "handler" (proves case:no/yes differ).
    assert _paths(seeded.conn, "case:yes Handler") == {"src/handler.go"}


# ------------------------------------------------------------------------------- regex


@pytest.mark.integration
def test_regex_case_insensitive_matches_any_letter_case(seeded: Seeded) -> None:
    assert _paths(seeded.conn, "/foo.*bar/") == {
        "src/escape1.go",
        "src/escape2.go",
        "src/regex_lower.go",
        "pkg/regex_upper.py",
    }


@pytest.mark.integration
def test_regex_case_sensitive_excludes_different_case_match(seeded: Seeded) -> None:
    # `pkg/regex_upper.py` contains "FooXXXBar" (uppercase F/B) -- the case:yes
    # variant must exclude it while case:no (above) included it.
    paths = _paths(seeded.conn, "case:yes /foo.*bar/")
    assert paths == {"src/escape1.go", "src/escape2.go", "src/regex_lower.go"}
    assert "pkg/regex_upper.py" not in paths


# ------------------------------------------------------------------------------ filters


@pytest.mark.integration
def test_repo_filter_restricts_to_matching_repo(seeded: Seeded) -> None:
    rows = _rows(seeded.conn, parse("repo:^acme"))
    assert {repo_id for repo_id, _ in rows} == {seeded.acme_id}
    assert {path for _, path in rows} == ACME_PATHS


@pytest.mark.integration
def test_path_filter_restricts_by_regex(seeded: Seeded) -> None:
    assert _paths(seeded.conn, "file:^pkg/") == BETA_PATHS


@pytest.mark.integration
def test_lang_filter_is_exact_and_excludes_null_lang_row(seeded: Seeded) -> None:
    # `src/no_lang.txt` has lang=NULL; `NULL = 'go'` is NULL (not true), so it is
    # correctly excluded even though its content otherwise matches nothing special.
    assert _paths(seeded.conn, "lang:go") == ACME_PATHS - {"src/no_lang.txt"}


@pytest.mark.integration
def test_lang_filter_unknown_value_returns_empty(seeded: Seeded) -> None:
    assert _paths(seeded.conn, "lang:cobol") == set()


@pytest.mark.integration
def test_symbol_filter_is_correlated_exists_join(seeded: Seeded) -> None:
    # Proves the EXISTS correlates on file_id (not a cartesian match-any-file bug):
    # each symbol name resolves to only the file that actually declares it.
    assert _paths(seeded.conn, "sym:Handler") == {"src/handler.go"}
    assert _paths(seeded.conn, "sym:helperFn") == {"src/util.go"}


# --------------------------------------------------------------------------- boolean


@pytest.mark.integration
def test_and_combination_returns_intersection(seeded: Seeded) -> None:
    # "handler" alone matches 4 files across both repos; AND lang:go narrows to the
    # 2 that are actually go (excluding the NULL-lang and python beta files).
    assert _paths(seeded.conn, "handler lang:go") == {"src/handler.go", "src/util.go"}


@pytest.mark.integration
def test_or_combination_returns_union(seeded: Seeded) -> None:
    # Union of the 4 "handler" matches and the 1 "foo_bar" match (5 distinct files;
    # proves OR is not silently collapsing to an AND/intersection).
    assert _paths(seeded.conn, "handler OR foo_bar") == {
        "src/handler.go",
        "src/util.go",
        "src/no_lang.txt",
        "pkg/handler_note.py",
        "src/escape1.go",
    }


# -------------------------------------------------------------------------- escaping


@pytest.mark.integration
def test_substring_escapes_literal_underscore_not_a_wildcard(seeded: Seeded) -> None:
    # `_` is a LIKE/ILIKE single-char wildcard; escaped so `foo_bar` matches only the
    # literal underscore file and NOT `fooXbar` (which an unescaped `_` would match).
    assert _paths(seeded.conn, "foo_bar") == {"src/escape1.go"}


@pytest.mark.integration
def test_null_content_row_excluded_by_content_predicates(seeded: Seeded) -> None:
    # `gamma/null_content.txt` has content=NULL. `NULL ILIKE ...` and `NULL ~* ...`
    # both evaluate to NULL (not true), so a content term never returns the row --
    # even though it is reachable via a path predicate (path is NOT NULL).
    assert "gamma/null_content.txt" in _paths(seeded.conn, "file:^gamma/")
    assert "gamma/null_content.txt" not in _paths(seeded.conn, "handler")  # ILIKE path
    assert "gamma/null_content.txt" not in _paths(seeded.conn, "/./")  # regex ~* path


# --------------------------------------------------------------- case override / resolve_case


@pytest.mark.integration
def test_filter_only_case_yes_defaults_insensitive(seeded: Seeded) -> None:
    # No content/regex leaf to derive the global case flag from -> path filter
    # defaults to `~*` (insensitive), so the differently-cased path IS included.
    assert "SRC/UpperCase.go" in _paths(seeded.conn, "case:yes file:^src/")


@pytest.mark.integration
def test_case_sensitive_override_makes_filter_only_query_exact(seeded: Seeded) -> None:
    query = "case:yes file:^src/"
    node = parse(query)
    lowercase_only = ACME_PATHS - {"SRC/UpperCase.go"}

    overridden = {path for _repo_id, path in _rows(seeded.conn, node, case_sensitive=True)}
    assert overridden == lowercase_only
    assert "SRC/UpperCase.go" not in overridden

    # A caller holding the raw query string gets the identical exact result via
    # resolve_case(query) instead of hardcoding True (Option D / KD-1).
    assert resolve_case(query) is True
    via_resolve_case = {
        path for _repo_id, path in _rows(seeded.conn, node, case_sensitive=resolve_case(query))
    }
    assert via_resolve_case == overridden


# ------------------------------------------------------------------------ limit / ordering


@pytest.mark.integration
def test_limit_and_ordering_are_repo_id_then_path(seeded: Seeded) -> None:
    node = parse("handler")  # matches 4 files spanning both seeded repos
    full = _rows(seeded.conn, node)
    assert full == [
        (seeded.acme_id, "src/handler.go"),
        (seeded.acme_id, "src/no_lang.txt"),
        (seeded.acme_id, "src/util.go"),
        (seeded.beta_id, "pkg/handler_note.py"),
    ]

    capped = _rows(seeded.conn, node, limit=2)
    assert capped == full[:2]


# ------------------------------------------------------------------- EXPLAIN / GIN usage


def _explain_plan(conn: Connection, node: Node) -> dict[str, Any]:
    """Render EXPLAIN (FORMAT JSON) for ``node`` with the seq-scan escape hatch
    disabled, scoped to a SAVEPOINT so the ``enable_seqscan`` GUC change never
    leaks into other tests sharing this module-scoped connection (``SET LOCAL`` is
    reverted by ``ROLLBACK TO SAVEPOINT``, per Postgres semantics).

    Inlines bound params via ``literal_binds`` so the plain ``EXPLAIN`` statement
    needs no separate parameter binding. Returns the root ``Plan`` node.
    """
    stmt = compile_query(node)
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    savepoint = conn.begin_nested()
    try:
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        raw = conn.execute(text(f"EXPLAIN (FORMAT JSON) {sql}")).scalar_one()
    finally:
        savepoint.rollback()
    plan_list = json.loads(raw) if isinstance(raw, str) else raw
    plan: dict[str, Any] = plan_list[0]["Plan"]
    return plan


def _plan_nodes(plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Recursively walk the EXPLAIN JSON plan tree (top-level node types are not
    enough -- the interesting Bitmap Index Scan is typically a child node)."""
    yield plan
    for child in plan.get("Plans", []) or []:
        yield from _plan_nodes(child)


def _uses_index(plan: dict[str, Any], index_name: str) -> bool:
    return any(node.get("Index Name") == index_name for node in _plan_nodes(plan))


@pytest.mark.integration
def test_explain_trigram_extractable_regex_uses_gin_index(seeded: Seeded) -> None:
    """AC3, the deterministic proving assertion.

    ``/Handler.*Request/`` has >=1 literal 3-gram, so with ``enable_seqscan = off``
    forcing the planner off its seq-scan escape hatch, Postgres MUST reach for
    ``ix_files_content_trgm`` (a Bitmap Index Scan feeding a Bitmap Heap Scan, or an
    Index Scan) -- this holds independent of row count / table statistics, so no
    large filler seed or ``ANALYZE`` is required for determinism.
    """
    plan = _explain_plan(seeded.conn, parse("/Handler.*Request/"))
    assert _uses_index(plan, "ix_files_content_trgm"), (
        f"expected a Bitmap/Index Scan on ix_files_content_trgm, got plan: {plan}"
    )


@pytest.mark.integration
@pytest.mark.xfail(
    strict=False,
    reason="Short/anchored regex (<3-gram literal content, or anchored) may not be "
    "trgm-extractable; whether the planner still reaches ix_files_content_trgm is "
    "Postgres-version/pg_trgm-extraction-dependent. Documents expected non-"
    "acceleration, not a regression -- XPASS or XFAIL either way, never fails the build.",
)
def test_short_anchored_regex_index_usage_is_warn_only(seeded: Seeded) -> None:
    plan = _explain_plan(seeded.conn, parse("/^Ha/"))
    assert not _uses_index(plan, "ix_files_content_trgm")


@pytest.mark.integration
@pytest.mark.xfail(
    strict=False,
    reason="`files.lang` carries no trgm index by design (KD-3) -- a bare lang: "
    "filter's predicate never touches `content`, so it cannot reach "
    "ix_files_content_trgm. Documents the by-design unindexed predicate, not a "
    "regression -- does not gate the build.",
)
def test_bare_lang_filter_index_usage_is_warn_only(seeded: Seeded) -> None:
    plan = _explain_plan(seeded.conn, parse("lang:go"))
    assert not _uses_index(plan, "ix_files_content_trgm")


@pytest.mark.integration
@pytest.mark.xfail(
    strict=False,
    reason="`repos.name` carries no trgm index by design -- a bare repo: filter's "
    "predicate never touches `files.content`, so it cannot reach "
    "ix_files_content_trgm. Documents the by-design unindexed predicate, not a "
    "regression -- does not gate the build.",
)
def test_bare_repo_filter_index_usage_is_warn_only(seeded: Seeded) -> None:
    plan = _explain_plan(seeded.conn, parse("repo:acme"))
    assert not _uses_index(plan, "ix_files_content_trgm")
