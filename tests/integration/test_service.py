"""Integration tests for ``search_code_payload``'s keyset-cursor pagination.

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
from app.db.models import Base, File, ReferenceEdge, Repo, Symbol
from indexer.hashing import content_sha

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

        handler_content = "package main\nfunc Handler() {}\n// foo lives here\n"
        handler_id = admin_conn.execute(
            insert(File)
            .values(
                repo_id=acme_id,
                path="src/00_handler.go",
                lang="go",
                content=handler_content,
                content_sha=content_sha(handler_content),
                branches=["HEAD"],
            )
            .returning(File.id)
        ).scalar_one()
        admin_conn.execute(
            insert(Symbol).values(
                file_id=handler_id, repo_id=acme_id, name="Handler", kind="function", start_line=2
            )
        )
        for path in ["src/a.go", "src/b.go", "src/c.go"]:
            content = f"// foo in {path}\n"
            admin_conn.execute(
                insert(File).values(
                    repo_id=acme_id,
                    path=path,
                    lang="go",
                    content=content,
                    content_sha=content_sha(content),
                    branches=["HEAD"],
                )
            )
        note_content = "# foo note\n"
        admin_conn.execute(
            insert(File).values(
                repo_id=beta_id,
                path="pkg/note.py",
                lang="python",
                content=note_content,
                content_sha=content_sha(note_content),
                branches=["HEAD"],
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
def test_invalid_regex_returns_recoverable_regex_invalid_envelope(seeded: Seeded) -> None:
    # The exact scenario issue #75 reports as a fault: a Postgres-invalid regex through the
    # full search_code_payload stack, end-to-end against real Postgres, must come back as a
    # recoverable envelope field -- never an uncaught DataError.
    payload = service.search_code_payload(seeded.engine, seeded.cfg, "/[/", 200)

    assert payload["regex_invalid"] is not None
    assert "invalid regular expression" in payload["regex_invalid"]
    assert payload["files"] == []
    assert payload["file_count"] == 0
    assert payload["truncated"] is False
    assert payload["query_too_broad"] is False


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


@pytest.mark.integration
def test_sym_only_query_with_candidates_over_row_limit_still_returns_single_page(
    seeded: Seeded,
) -> None:
    # Regression (review finding): a sym: name shared by MORE files than `row_limit` row-caps
    # grep's own CANDIDATE scan (it evaluates the sym: EXISTS predicate to find candidates, same
    # as the compiler) even though grep's `files` is always empty for a filter-only query -- so
    # without the fix, next_cursor came out non-null and every continuation page (which skips
    # the page-1-only symbol leg too) would fetch another empty page forever. `row_limit=2`
    # against 3 Thing-symbol files reproduces the row-capped candidate scan.
    with seeded.engine.connect() as conn:
        for i in range(3):
            thing_content = "package main\n"
            file_id = conn.execute(
                insert(File)
                .values(
                    repo_id=seeded.acme_id,
                    path=f"src/thing{i}.go",
                    lang="go",
                    content=thing_content,
                    content_sha=content_sha(thing_content),
                    branches=["HEAD"],
                )
                .returning(File.id)
            ).scalar_one()
            conn.execute(
                insert(Symbol).values(
                    file_id=file_id,
                    repo_id=seeded.acme_id,
                    name="Thing",
                    kind="function",
                    start_line=1,
                )
            )
        conn.commit()

    payload = service.search_code_payload(seeded.engine, seeded.cfg, "sym:Thing", 2, cursor=None)
    assert payload["next_cursor"] is None
    # The "there's more" signal is still surfaced -- just not via next_cursor. The symbol
    # leg's OWN candidate scan is row_limit-capped identically, so with 3 matches and
    # row_limit=2 it can't return all of them either.
    assert payload["truncated"] is True
    assert payload["truncation_reason"] == "row_cap"


# ------------------------------------------------------------------- negation (issue #70)


@pytest.mark.integration
def test_negative_only_query_with_candidates_over_row_limit_still_returns_single_page(
    seeded: Seeded,
) -> None:
    # The negation analog of
    # test_sym_only_query_with_candidates_over_row_limit_still_returns_single_page: "-bar" is
    # no_content_atom=True at the grep layer exactly like a filter-only query, and grep's own
    # candidate scan for it can still row-cap (every "foo"-file here also satisfies "-bar",
    # since none contain "bar"). Without suppression this would leak a non-null next_cursor and
    # every continuation page (there is nothing to highlight, ever) would replay another empty
    # page forever.
    payload = service.search_code_payload(seeded.engine, seeded.cfg, "-bar", 2, cursor=None)
    assert payload["next_cursor"] is None
    assert payload["no_content_atom"] is True
    assert payload["file_count"] == 0


@pytest.mark.integration
def test_negative_content_atom_excludes_matching_file_end_to_end(seeded: Seeded) -> None:
    content = "foo and bar together\n"
    with seeded.engine.connect() as conn:
        conn.execute(
            insert(File).values(
                repo_id=seeded.acme_id,
                path="src/z_both.go",
                lang="go",
                content=content,
                content_sha=content_sha(content),
                branches=["HEAD"],
            )
        )
        conn.commit()

    payload = service.search_code_payload(seeded.engine, seeded.cfg, "foo -bar", 200)
    files = _files(payload)
    assert "src/z_both.go" not in files
    assert set(files) == {"src/00_handler.go", "src/a.go", "src/b.go", "src/c.go", "pkg/note.py"}


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


# ------------------------------------------------- permalink_branch selection
#
# A dedicated, function-scoped fixture (not the module-scoped `seeded` above): mirrors the
# divergent-content corpus shape from tests/integration/test_query_compiler.py's
# `branch_seeded` fixture (src/divergent.go with DIFFERENT content on "main" vs "feature") so
# a lexical search can hit both content versions of one path in a single query.


class BranchSeeded(NamedTuple):
    engine: Engine
    cfg: Settings
    repo_id: int


@pytest.fixture
def branch_seeded() -> Iterator[BranchSeeded]:
    schema = _unique(f"{SCHEMA_PREFIX}_branch")
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

        repo_id = admin_conn.execute(
            insert(Repo).values(name="acme/divergent", default_branch="main").returning(Repo.id)
        ).scalar_one()

        # Same path, DIFFERENT content on "main" vs "feature" -> two distinct rows (0003
        # dedup: same repo_id+path, different content_sha), each with a single-element
        # branches array.
        main_content = 'package main\nfunc Divergent() { fmt.Println("main") }\n'
        feature_content = 'package main\nfunc Divergent() { fmt.Println("feature") }\n'
        admin_conn.execute(
            insert(File).values(
                repo_id=repo_id,
                path="src/divergent.go",
                lang="go",
                content=main_content,
                content_sha=content_sha(main_content),
                branches=["main"],
            )
        )
        admin_conn.execute(
            insert(File).values(
                repo_id=repo_id,
                path="src/divergent.go",
                lang="go",
                content=feature_content,
                content_sha=content_sha(feature_content),
                branches=["feature"],
            )
        )
        admin_conn.commit()

        os.environ["PGOPTIONS"] = f"-c search_path={schema},public"
        engine = create_db_engine()  # fresh engine: every NEW connection honors PGOPTIONS
        yield BranchSeeded(engine=engine, cfg=_cfg(), repo_id=repo_id)
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


@pytest.mark.integration
def test_permalink_branch_lexical_search_matches_both_divergent_versions(
    branch_seeded: BranchSeeded,
) -> None:
    # Parenthesized OR of branch: atoms ANDed with a content term (NOT a bare
    # "branch:main OR branch:feature Divergent", which the grammar would parse as
    # "branch:main OR (branch:feature AND Divergent)" -- an unbalanced disjunction): both
    # content versions surface, each carrying a DISTINCT content_sha.
    payload = service.search_code_payload(
        branch_seeded.engine, branch_seeded.cfg, "(branch:main OR branch:feature) Divergent", 200
    )
    assert payload["file_count"] == 2
    shas = {f["content_sha"] for f in payload["files"]}
    assert len(shas) == 2


@pytest.mark.integration
def test_permalink_branch_set_from_explicit_branch_atom(branch_seeded: BranchSeeded) -> None:
    payload = service.search_code_payload(
        branch_seeded.engine, branch_seeded.cfg, "branch:feature Divergent", 200
    )
    assert payload["file_count"] == 1
    assert payload["files"][0]["permalink_branch"] == "feature"


@pytest.mark.integration
def test_permalink_branch_none_when_query_has_no_branch_atom(branch_seeded: BranchSeeded) -> None:
    # No branch: atom anywhere -> the implicit default-branch conjunct scopes to "main" only,
    # and permalink_branch must be None for every returned entry.
    payload = service.search_code_payload(branch_seeded.engine, branch_seeded.cfg, "Divergent", 200)
    assert payload["file_count"] == 1
    for f in payload["files"]:
        assert f["permalink_branch"] is None


@pytest.mark.integration
def test_permalink_branch_or_multi_branch_picks_smallest_intersection_and_round_trips(
    branch_seeded: BranchSeeded,
) -> None:
    # An OR of two branch: atoms over a row whose OWN membership contains only ONE of them
    # still has BOTH values in branch_filters; the intersection with that row's single-element
    # branches array is exactly that one value, so it is trivially the "smallest".
    payload = service.search_code_payload(
        branch_seeded.engine, branch_seeded.cfg, "(branch:feature OR branch:main) Divergent", 200
    )
    by_branches = {tuple(f["branches"]): f for f in payload["files"]}
    feature_entry = by_branches[("feature",)]
    assert feature_entry["permalink_branch"] == "feature"

    # Round-trip proof: get_file_payload(branch=permalink_branch) returns the SAME content
    # bytes as the search hit's own version.
    file_payload = service.get_file_payload(
        branch_seeded.engine,
        branch_seeded.cfg,
        "acme/divergent",
        "src/divergent.go",
        branch=feature_entry["permalink_branch"],
    )
    assert file_payload["found"] is True
    content = file_payload["content"] or ""
    assert 'fmt.Println("feature")' in content
    assert 'fmt.Println("main")' not in content


# ---------------------------------------- find_references_payload / list_imports_payload


class RefSeeded(NamedTuple):
    engine: Engine
    cfg: Settings
    acme_id: int
    beta_id: int


@pytest.fixture
def ref_seeded() -> Iterator[RefSeeded]:
    """Same PGOPTIONS idiom as ``seeded``, with a small call/import edge + symbol corpus."""
    schema = _unique(f"{SCHEMA_PREFIX}_ref")
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
            insert(Repo).values(name="acme/widgets", default_branch="main").returning(Repo.id)
        ).scalar_one()
        beta_id = admin_conn.execute(
            insert(Repo).values(name="beta/tools", default_branch="main").returning(Repo.id)
        ).scalar_one()

        def _file(repo_id: int, path: str) -> int:
            content = f"# {path}\n"
            return admin_conn.execute(
                insert(File)
                .values(
                    repo_id=repo_id,
                    path=path,
                    lang="python",
                    content=content,
                    content_sha=content_sha(content),
                    branches=["main"],
                )
                .returning(File.id)
            ).scalar_one()

        target_id = _file(acme_id, "src/target.py")
        admin_conn.execute(
            insert(Symbol).values(
                file_id=target_id, repo_id=acme_id, name="Handler", kind="function", start_line=2
            )
        )
        caller_id = _file(acme_id, "src/caller.py")
        admin_conn.execute(
            insert(ReferenceEdge).values(
                file_id=caller_id,
                repo_id=acme_id,
                edge_kind="call",
                target_name="Handler",
                line=5,
                enclosing_name="run",
                enclosing_kind="function",
            )
        )
        importer_id = _file(acme_id, "src/importer.py")
        admin_conn.execute(
            insert(ReferenceEdge).values(
                file_id=importer_id,
                repo_id=acme_id,
                edge_kind="import",
                target_name="os.path",
                line=1,
            )
        )
        admin_conn.commit()

        os.environ["PGOPTIONS"] = f"-c search_path={schema},public"
        engine = create_db_engine()
        yield RefSeeded(engine=engine, cfg=_cfg(), acme_id=acme_id, beta_id=beta_id)
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


@pytest.mark.integration
def test_find_references_payload_end_to_end_wire_shape(ref_seeded: RefSeeded) -> None:
    payload = service.find_references_payload(ref_seeded.engine, ref_seeded.cfg, "Handler", 200)

    assert payload["query"] == "Handler"
    assert payload["kind"] == "references"
    assert payload["site_count"] == 1
    assert payload["resolution_summary"] == {"unique": 1, "ambiguous": 0, "unresolved": 0}
    (site,) = payload["sites"]
    assert site["repo"] == "acme/widgets"
    assert site["file"] == "src/caller.py"
    assert site["enclosing_symbol"] == {"name": "run", "kind": "function"}
    (candidate,) = site["candidates"]
    assert candidate["repo"] == "acme/widgets"
    assert candidate["file"] == "src/target.py"
    assert candidate["same_repo"] is True
    assert "symbol_id" not in candidate


@pytest.mark.integration
def test_list_imports_payload_end_to_end_wire_shape(ref_seeded: RefSeeded) -> None:
    payload = service.list_imports_payload(ref_seeded.engine, ref_seeded.cfg, "acme/widgets", 200)

    assert payload["kind"] == "imports"
    assert payload["repo"] == "acme/widgets"
    assert payload["repo_known"] is True
    assert payload["site_count"] == 1
    assert payload["resolution_summary"] == {"unique": 0, "ambiguous": 0, "unresolved": 1}
    (site,) = payload["sites"]
    assert site["edge_kind"] == "import"
    assert site["target_name"] == "os.path"


@pytest.mark.integration
def test_list_imports_payload_unknown_repo_against_real_corpus(ref_seeded: RefSeeded) -> None:
    payload = service.list_imports_payload(ref_seeded.engine, ref_seeded.cfg, "ghost/repo", 200)

    assert payload["repo_known"] is False
    assert payload["sites"] == []
    assert payload["resolution_summary"] == {"unique": 0, "ambiguous": 0, "unresolved": 0}


@pytest.mark.integration
def test_list_imports_payload_imported_by_finds_importer(ref_seeded: RefSeeded) -> None:
    # "who imports os.path" -- corpus-wide over ix_reference_edges_target_name; the seeded
    # importer site comes back with no repo scope requested (repo_known always True).
    payload = service.list_imports_payload(
        ref_seeded.engine, ref_seeded.cfg, target="os.path", direction="imported_by"
    )

    assert payload["kind"] == "imports"
    assert payload["direction"] == "imported_by"
    assert payload["target"] == "os.path"
    assert payload["repo"] is None
    assert payload["repo_known"] is True
    assert payload["site_count"] == 1
    (site,) = payload["sites"]
    assert site["repo"] == "acme/widgets"
    assert site["file"] == "src/importer.py"
    assert site["line"] == 1
    assert site["edge_kind"] == "import"


@pytest.mark.integration
def test_list_imports_payload_imported_by_unknown_target_is_empty_not_error(
    ref_seeded: RefSeeded,
) -> None:
    payload = service.list_imports_payload(
        ref_seeded.engine, ref_seeded.cfg, target="nonexistent.module", direction="imported_by"
    )

    assert payload["query_too_broad"] is False
    assert payload["repo_known"] is True
    assert payload["sites"] == []
    assert payload["site_count"] == 0
    assert payload["resolution_summary"] == {"unique": 0, "ambiguous": 0, "unresolved": 0}


@pytest.mark.integration
def test_list_imports_payload_imported_by_repo_narrowing(ref_seeded: RefSeeded) -> None:
    # The seeded importer is in acme/widgets, so narrowing to it keeps the site; narrowing to
    # beta/tools (which has no such import) drops it -- a known repo with zero matching sites.
    acme = service.list_imports_payload(
        ref_seeded.engine,
        ref_seeded.cfg,
        "acme/widgets",
        200,
        target="os.path",
        direction="imported_by",
    )
    assert acme["repo"] == "acme/widgets"
    assert acme["repo_known"] is True
    assert acme["site_count"] == 1

    beta = service.list_imports_payload(
        ref_seeded.engine,
        ref_seeded.cfg,
        "beta/tools",
        200,
        target="os.path",
        direction="imported_by",
    )
    assert beta["repo_known"] is True
    assert beta["sites"] == []
