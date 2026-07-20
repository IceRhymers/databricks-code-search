"""Integration tests for ``search_code_payload``'s keyset-cursor pagination (issue #35 A2/A3).

Requires a running Postgres with the standard PG* env set. Mirrors ``test_mcp_server.py``'s
PGOPTIONS idiom rather than ``test_grep.py``'s single-connection ``seeded`` fixture:
``search_code_payload`` takes an ``Engine`` and opens its OWN connection per call (mirroring
production), so the throwaway schema must be visible to every NEW connection the engine's pool
hands out, not just one held-open connection. Setting ``PGOPTIONS`` before building the engine
makes libpq apply ``search_path`` to every connection it opens.

In this repo that Postgres exists only as CI's service container, so these tests are CI-only
and were validated locally by lint/type-check + ``--collect-only``, not execution.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import NamedTuple

import pytest
from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from app import service
from app.config import Settings
from app.db.client import create_db_engine
from app.db.models import Base, File, Repo, Symbol

SCHEMA_PREFIX = "test_service"


class Seeded(NamedTuple):
    engine: Engine
    cfg: Settings
    acme_id: int
    beta_id: int


def _unique(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _cfg() -> Settings:
    return Settings(
        lakebase_endpoint=None,
        statement_timeout_ms=5000,
        max_content_bytes=8 * 1024 * 1024,
        row_limit=200,
        max_row_limit=1000,
        semantic_enabled=False,
    )


@pytest.fixture
def seeded() -> Iterator[Seeded]:
    """Throwaway schema visible to a dedicated engine (via PGOPTIONS), plus a small corpus.

    Corpus: five "foo"-matching files across two repos (acme has four, beta has one), so
    ``(repo_id, path)`` order is deterministic and small ``limit`` values force multiple pages.
    ``src/00_handler.go`` also carries a ``Handler`` symbol definition -- named to sort FIRST
    among acme's files so it lands in page 1's candidate window for BOTH grep_search's content
    query and symbol_search's own (identically ``row_limit``-capped, uncursored) candidate
    scan; that page-1-inclusion guarantee, not the symbol itself, is what the mixed sym+content
    pagination tests below depend on.
    """
    schema = _unique(SCHEMA_PREFIX)
    admin_engine = create_db_engine()
    admin_conn = admin_engine.connect()
    prev_pgoptions = os.environ.get("PGOPTIONS")
    engine: Engine | None = None
    try:
        admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin_conn.execute(text(f"CREATE SCHEMA {schema}"))
        admin_conn.execute(text(f"SET search_path TO {schema}, public"))
        admin_conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        admin_conn.commit()

        Base.metadata.create_all(bind=admin_conn)
        admin_conn.commit()

        acme_id = admin_conn.execute(
            insert(Repo).values(name="acme/widgets").returning(Repo.id)
        ).scalar_one()
        beta_id = admin_conn.execute(
            insert(Repo).values(name="beta/tools").returning(Repo.id)
        ).scalar_one()

        handler_id = admin_conn.execute(
            insert(File)
            .values(
                repo_id=acme_id,
                path="src/00_handler.go",
                lang="go",
                content="package main\nfunc Handler() {}\n// foo lives here\n",
            )
            .returning(File.id)
        ).scalar_one()
        admin_conn.execute(
            insert(Symbol).values(
                file_id=handler_id, repo_id=acme_id, name="Handler", kind="function", start_line=2
            )
        )
        for path in ["src/a.go", "src/b.go", "src/c.go"]:
            admin_conn.execute(
                insert(File).values(
                    repo_id=acme_id, path=path, lang="go", content=f"// foo in {path}\n"
                )
            )
        admin_conn.execute(
            insert(File).values(
                repo_id=beta_id, path="pkg/note.py", lang="python", content="# foo note\n"
            )
        )
        admin_conn.commit()

        os.environ["PGOPTIONS"] = f"-c search_path={schema},public"
        engine = create_db_engine()  # fresh engine: every NEW connection honors PGOPTIONS
        yield Seeded(engine=engine, cfg=_cfg(), acme_id=acme_id, beta_id=beta_id)
    finally:
        if engine is not None:
            engine.dispose()
        if prev_pgoptions is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = prev_pgoptions
        admin_conn.rollback()
        admin_conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        admin_conn.commit()
        admin_conn.close()
        admin_engine.dispose()


def _files(payload: dict[str, object]) -> list[str]:
    files = payload["files"]
    assert isinstance(files, list)
    return [f["file"] for f in files]  # type: ignore[index]


# --------------------------------------------------------------------------- basic shape


@pytest.mark.integration
def test_bare_call_has_no_next_cursor_key(seeded: Seeded) -> None:
    payload = service.search_code_payload(seeded.engine, seeded.cfg, "foo", 200)
    assert "next_cursor" not in payload
    assert payload["file_count"] == 5  # handler.go, a.go, b.go, c.go, note.py


@pytest.mark.integration
def test_page_one_folds_symbol_and_content_matches(seeded: Seeded) -> None:
    payload = service.search_code_payload(
        seeded.engine, seeded.cfg, "sym:Handler OR foo", 10, cursor=None
    )
    (handler,) = [f for f in payload["files"] if f["file"] == "src/00_handler.go"]
    kinds = {m.get("symbols", [{}])[0].get("name") for m in handler["matches"] if "symbols" in m}
    assert kinds == {"Handler"}


# --------------------------------------------------------------- disjoint deterministic pages


@pytest.mark.integration
def test_pagination_walk_covers_every_file_exactly_once(seeded: Seeded) -> None:
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        payload = service.search_code_payload(seeded.engine, seeded.cfg, "foo", 2, cursor=cursor)
        seen.extend(_files(payload))
        cursor = payload["next_cursor"]
        if cursor is None:
            break
    else:
        pytest.fail("pagination did not exhaust within 10 pages")
    assert sorted(seen) == sorted(
        ["src/00_handler.go", "src/a.go", "src/b.go", "src/c.go", "pkg/note.py"]
    )
    assert len(seen) == len(set(seen))  # no duplicates across pages


@pytest.mark.integration
def test_pagination_pages_are_disjoint_and_deterministic_across_two_runs(seeded: Seeded) -> None:
    def _walk() -> list[str]:
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):
            payload = service.search_code_payload(
                seeded.engine, seeded.cfg, "foo", 2, cursor=cursor
            )
            seen.extend(_files(payload))
            cursor = payload["next_cursor"]
            if cursor is None:
                break
        return seen

    assert _walk() == _walk()  # same order every time -- deterministic keyset order


# ---------------------------------------------------------------- mixed sym+content query


@pytest.mark.integration
def test_mixed_sym_content_query_symbols_only_on_page_one(seeded: Seeded) -> None:
    query = "sym:Handler OR foo"
    page1 = service.search_code_payload(seeded.engine, seeded.cfg, query, 2, cursor=None)
    assert page1["next_cursor"] is not None

    def _symbol_names(payload: dict[str, object]) -> list[str]:
        names: list[str] = []
        for f in payload["files"]:  # type: ignore[union-attr]
            for m in f["matches"]:
                for sym in m.get("symbols", []):
                    names.append(sym["name"])
        return names

    assert _symbol_names(page1) == ["Handler"]

    seen_symbols: list[str] = list(_symbol_names(page1))
    cursor = page1["next_cursor"]
    for _ in range(10):
        if cursor is None:
            break
        payload = service.search_code_payload(seeded.engine, seeded.cfg, query, 2, cursor=cursor)
        seen_symbols.extend(_symbol_names(payload))
        cursor = payload["next_cursor"]

    # Exactly one Handler symbol across the ENTIRE walk -- folded once, on page 1 only.
    assert seen_symbols == ["Handler"]


@pytest.mark.integration
def test_mixed_sym_content_query_shape_flags_stable_across_pages(seeded: Seeded) -> None:
    # `sym:Handler OR foo` has a real content atom ("foo"), so neither shape flag is ever
    # expected to fire -- pins that folding/skipping the symbol leg across pages never flips
    # them.
    query = "sym:Handler OR foo"
    cursor: str | None = None
    pages = 0
    for _ in range(10):
        payload = service.search_code_payload(seeded.engine, seeded.cfg, query, 2, cursor=cursor)
        assert payload["no_content_atom"] is False
        assert payload["zero_width_only_atoms"] is False
        pages += 1
        cursor = payload["next_cursor"]
        if cursor is None:
            break
    assert pages >= 2  # actually exercised multiple pages, not just page 1


@pytest.mark.integration
def test_sym_only_query_returns_single_page(seeded: Seeded) -> None:
    payload = service.search_code_payload(
        seeded.engine, seeded.cfg, "sym:Handler", 200, cursor=None
    )
    assert payload["next_cursor"] is None
    assert payload["file_count"] == 1


# --------------------------------------------------------------------------- cursor errors


@pytest.mark.integration
def test_garbled_cursor_raises_cursor_error(seeded: Seeded) -> None:
    with pytest.raises(service.CursorError):
        service.search_code_payload(seeded.engine, seeded.cfg, "foo", 200, cursor="not-a-cursor!!!")


@pytest.mark.integration
def test_stale_cursor_from_a_different_query_still_decodes_and_resumes(seeded: Seeded) -> None:
    # Cursor validity is opaque-value shaped, not query-bound: decoding never inspects `query`.
    # A structurally valid cursor for a DIFFERENT (repo_id, path) resumes from wherever it
    # points, which is the documented contract -- not an error.
    page1 = service.search_code_payload(seeded.engine, seeded.cfg, "foo", 2, cursor=None)
    reused = service.search_code_payload(
        seeded.engine, seeded.cfg, "foo", 2, cursor=page1["next_cursor"]
    )
    assert isinstance(reused["files"], list)
