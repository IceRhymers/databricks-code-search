"""Integration tests for symbol search (issue #13): query -> executed SQL -> symbol defs.

Requires a running Postgres with the standard PG* env set. Mirrors the throwaway-schema idiom
of ``tests/integration/test_grep.py`` / ``test_query_compiler.py`` (unique schema, ``SET
search_path``, ``CREATE EXTENSION pg_trgm``, ``Base.metadata.create_all`` on the same
connection, ``DROP SCHEMA ... CASCADE`` + ``engine.dispose()`` in ``finally``). In this repo
that Postgres exists only as CI's service container, so these tests are CI-only and were
validated locally by lint/type-check + ``--collect-only``, not execution.

The ``seeded`` fixture is function-scoped: the timeout test inserts large blob rows and the
determinism/eligibility assertions rely on a clean corpus, so each test gets its own.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import NamedTuple

import pytest
from sqlalchemy import Connection, insert, text

from app.db.client import create_db_engine
from app.db.models import Base, File, Repo, Symbol
from app.search.errors import QueryTooBroadError
from app.search.symbols import SymbolResult, symbol_search

SCHEMA_PREFIX = "test_symsearch"


class Seeded(NamedTuple):
    conn: Connection
    acme_id: int
    beta_id: int
    files: dict[str, int]


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _insert_repo(conn: Connection, name: str) -> int:
    return conn.execute(insert(Repo).values(name=name).returning(Repo.id)).scalar_one()


def _insert_file(
    conn: Connection, repo_id: int, path: str, *, lang: str | None, content: str | None
) -> int:
    return conn.execute(
        insert(File)
        .values(repo_id=repo_id, path=path, lang=lang, content=content)
        .returning(File.id)
    ).scalar_one()


def _insert_symbol(
    conn: Connection,
    file_id: int,
    repo_id: int,
    name: str,
    *,
    kind: str | None = "function",
    start_line: int | None = 1,
) -> int:
    return conn.execute(
        insert(Symbol)
        .values(file_id=file_id, repo_id=repo_id, name=name, kind=kind, start_line=start_line)
        .returning(Symbol.id)
    ).scalar_one()


@pytest.fixture
def seeded() -> Iterator[Seeded]:
    """Throwaway schema + durable-core DDL + a deterministic symbol corpus."""
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
                "package main\n\nfunc Handler() {}\n// foo lives here\nfunc parseRequest() {}\n"
            ),
        )
        _insert_symbol(conn, files["src/handler.go"], acme_id, "Handler", start_line=3)
        _insert_symbol(conn, files["src/handler.go"], acme_id, "parseRequest", start_line=5)

        files["src/util.go"] = _insert_file(
            conn,
            acme_id,
            "src/util.go",
            lang="go",
            content="package main\n\nfunc helperFn() {}\ntype handler struct{}\n",
        )
        _insert_symbol(conn, files["src/util.go"], acme_id, "helperFn", start_line=3)
        # Lowercase `handler` type -> case-insensitive sym:Handler matches it; case:yes excludes.
        _insert_symbol(conn, files["src/util.go"], acme_id, "handler", kind="type", start_line=4)

        files["src/dup.go"] = _insert_file(
            conn,
            acme_id,
            "src/dup.go",
            lang="go",
            content="func Overload() {}\n// ...\nfunc Overload() {}\n",
        )
        # Two same-named symbols -> no dedup; ordered by start_line (id tiebreak).
        _insert_symbol(conn, files["src/dup.go"], acme_id, "Overload", start_line=1)
        _insert_symbol(conn, files["src/dup.go"], acme_id, "Overload", start_line=3)

        files["src/nulline.go"] = _insert_file(
            conn, acme_id, "src/nulline.go", lang="go", content="func NoLine() {}\n"
        )
        _insert_symbol(conn, files["src/nulline.go"], acme_id, "NoLine", start_line=None)

        # beta: "Handler" appears in CONTENT only, with NO Handler symbol row (A-vs-B exclusion).
        files["pkg/note.py"] = _insert_file(
            conn,
            beta_id,
            "pkg/note.py",
            lang="python",
            content="# Handler is mentioned in this comment only\n",
        )
        conn.commit()

        yield Seeded(conn=conn, acme_id=acme_id, beta_id=beta_id, files=files)
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


def _search(conn: Connection, query: str, **kwargs: object) -> SymbolResult:
    return symbol_search(conn, query, **kwargs)  # type: ignore[arg-type]


def _pairs(result: SymbolResult) -> set[tuple[str, str]]:
    """(path, name) pairs, order-independent."""
    return {(sm.path, sm.name) for sm in result.symbols}


# --------------------------------------------------------------------------- definitions


@pytest.mark.integration
def test_sym_alone_returns_definition_with_file_and_line(seeded: Seeded) -> None:
    result = _search(seeded.conn, "sym:Handler")
    # Case-insensitive: matches the "Handler" function AND the lowercase "handler" type.
    assert _pairs(result) == {("src/handler.go", "Handler"), ("src/util.go", "handler")}
    handler = next(sm for sm in result.symbols if sm.name == "Handler")
    assert handler.start_line == 3
    assert handler.kind == "function"
    assert handler.lang == "go"
    assert result.no_symbol_atom is False


@pytest.mark.integration
def test_content_only_match_in_other_file_is_excluded(seeded: Seeded) -> None:
    # beta/note.py contains the word "Handler" in content but declares no Handler symbol,
    # so symbol search must NOT return it (eligibility is the symbol EXISTS, not content).
    result = _search(seeded.conn, "sym:Handler")
    assert all(sm.path != "pkg/note.py" for sm in result.symbols)


@pytest.mark.integration
def test_case_yes_sym_is_exact(seeded: Seeded) -> None:
    # case:yes flips both the file-eligibility EXISTS and the outer name match to `~`,
    # excluding the lowercase "handler" type that case:no included.
    result = _search(seeded.conn, "case:yes sym:Handler")
    assert _pairs(result) == {("src/handler.go", "Handler")}


@pytest.mark.integration
def test_repo_filter_scopes_symbols(seeded: Seeded) -> None:
    assert _pairs(_search(seeded.conn, "repo:^acme sym:Handler")) == {
        ("src/handler.go", "Handler"),
        ("src/util.go", "handler"),
    }
    # beta has no Handler symbol (only content) -> empty.
    assert _search(seeded.conn, "repo:^beta sym:Handler").symbols == ()


@pytest.mark.integration
def test_lang_filter_scopes_symbols(seeded: Seeded) -> None:
    assert _pairs(_search(seeded.conn, "lang:go sym:Handler")) == {
        ("src/handler.go", "Handler"),
        ("src/util.go", "handler"),
    }
    # python file has no Handler symbol -> empty.
    assert _search(seeded.conn, "lang:python sym:Handler").symbols == ()


@pytest.mark.integration
def test_content_substring_narrows_eligible_files_not_symbols(seeded: Seeded) -> None:
    # `foo` lives only in handler.go; util.go's lowercase "handler" symbol is dropped because
    # util.go is not content-eligible (proves content atoms narrow FILES, not which symbols).
    assert _pairs(_search(seeded.conn, "sym:Handler foo")) == {("src/handler.go", "Handler")}


@pytest.mark.integration
def test_regex_atom_narrows_eligible_files(seeded: Seeded) -> None:
    # `/lives/` matches handler.go content only; the outer name filter still keeps only Handler.
    assert _pairs(_search(seeded.conn, "sym:Handler /lives/")) == {("src/handler.go", "Handler")}


@pytest.mark.integration
def test_or_branch_without_sym_atom_is_inert(seeded: Seeded) -> None:
    # `sym:Handler OR lang:go` admits every go file via lang:go, but the outer name filter keeps
    # only Handler-named symbols -> identical output to `sym:Handler` alone.
    assert _pairs(_search(seeded.conn, "sym:Handler OR lang:go")) == _pairs(
        _search(seeded.conn, "sym:Handler")
    )


# ------------------------------------------------------------------------- edge behavior


@pytest.mark.integration
def test_no_sym_atom_short_circuits_empty(seeded: Seeded) -> None:
    result = _search(seeded.conn, "lang:go foo")
    assert result.symbols == ()
    assert result.no_symbol_atom is True


@pytest.mark.integration
def test_unknown_symbol_name_returns_empty_not_no_atom(seeded: Seeded) -> None:
    result = _search(seeded.conn, "sym:Nonexistent")
    assert result.symbols == ()
    assert result.no_symbol_atom is False


@pytest.mark.integration
def test_duplicate_names_are_not_deduped_and_order_is_stable(seeded: Seeded) -> None:
    result = _search(seeded.conn, "sym:Overload")
    overloads = [sm for sm in result.symbols if sm.name == "Overload"]
    assert len(overloads) == 2
    # Ordered by (repo_id, path, start_line, name, id): line 1 before line 3.
    assert [sm.start_line for sm in overloads] == [1, 3]


@pytest.mark.integration
def test_null_start_line_is_returned_as_none(seeded: Seeded) -> None:
    result = _search(seeded.conn, "sym:NoLine")
    (sym,) = result.symbols
    assert sym.name == "NoLine"
    assert sym.start_line is None


@pytest.mark.integration
def test_tiny_statement_timeout_raises_query_too_broad(seeded: Seeded) -> None:
    # Deterministic DB-cancellation by WORK VOLUME (mirrors test_grep.py): a 2-char regex is
    # unindexable by pg_trgm and matches nothing, so the candidate query full-scans ~16 MB of
    # content before LIMIT can short-circuit -- guaranteed >> 1 ms. The blob files each declare
    # a `blobsym` symbol so the sym: EXISTS admits them and the content regex actually runs.
    blob = "a" * (2 * 1024 * 1024)  # 2 MiB per file, 8 files -> ~16 MiB scanned
    for i in range(8):
        fid = _insert_file(seeded.conn, seeded.acme_id, f"src/blob{i}.txt", lang=None, content=blob)
        _insert_symbol(seeded.conn, fid, seeded.acme_id, "blobsym", start_line=1)
    seeded.conn.commit()
    with pytest.raises(QueryTooBroadError):
        _search(seeded.conn, "sym:blobsym /zq/", statement_timeout_ms=1)


@pytest.mark.integration
def test_healthy_query_does_not_raise_query_too_broad(seeded: Seeded) -> None:
    # Positive control: the timeout guard must not fire spuriously on a fast query.
    result = _search(seeded.conn, "sym:Handler")
    assert result.symbols
    assert result.truncated is False
