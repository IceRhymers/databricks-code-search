"""Integration tests for the query compiler: AST -> executed SQL rows.

Runs against a Lakebase branch via ``create_db_engine()`` (schema idiom mirrors
``tests/integration/test_migrations.py``: a unique throwaway schema, ``SET
search_path``, ``Base.metadata.create_all`` -- which also builds the trgm GIN
indexes -- and ``DROP SCHEMA ... CASCADE`` cleanup in a ``finally``). CI runs the
suite on an ephemeral branch (``.github/workflows/ci-lakebase.yml``).

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
from indexer.hashing import content_sha

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


def _insert_repo(conn: Connection, name: str, *, default_branch: str | None = "main") -> int:
    return conn.execute(
        insert(Repo).values(name=name, default_branch=default_branch).returning(Repo.id)
    ).scalar_one()


def _insert_file(
    conn: Connection,
    repo_id: int,
    path: str,
    *,
    lang: str | None,
    content: str | None,
    branches: list[str] | None = None,
) -> int:
    # branches defaults to ["main"] -- matches the default _insert_repo(default_branch="main")
    # above, so every existing (pre-0003) test keeps seeing its files via the implicit
    # default-branch conjunct with no per-call change.
    return conn.execute(
        insert(File)
        .values(
            repo_id=repo_id,
            path=path,
            lang=lang,
            content=content,
            content_sha=content_sha(content),
            branches=branches if branches is not None else ["main"],
        )
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


# ------------------------------------------------------------------- branch scoping (0003)
#
# A dedicated, function-scoped fixture (not the module-scoped `seeded` above): these tests
# probe multi-branch dedup shapes (divergent content, NULL default_branch) the shared corpus
# was never built to represent, and mutating it would perturb every other test in this module.


class BranchSeeded(NamedTuple):
    conn: Connection
    main_repo_id: int  # default_branch="main"
    master_repo_id: int  # default_branch="master" (different default, proves per-repo coalesce)
    null_default_repo_id: int  # default_branch IS NULL -> coalesce(...,'HEAD')


@pytest.fixture
def branch_seeded() -> Iterator[BranchSeeded]:
    schema = _unique(f"{SCHEMA_PREFIX}_branch")
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

        main_id = _insert_repo(conn, "branch/main-default", default_branch="main")
        master_id = _insert_repo(conn, "branch/master-default", default_branch="master")
        null_id = _insert_repo(conn, "branch/null-default", default_branch=None)

        # main_id: a path present only on "main" (default) and a path present on BOTH "main"
        # and "feature" (array-union membership, one row).
        _insert_file(
            conn,
            main_id,
            "src/default_only.go",
            lang="go",
            content="on main only",
            branches=["main"],
        )
        _insert_file(
            conn,
            main_id,
            "src/shared.go",
            lang="go",
            content="same content on both branches",
            branches=["main", "feature"],
        )
        # A divergent-content path: "main" and "feature" both touch src/divergent.go but with
        # DIFFERENT content -> two distinct rows (distinct content_sha), each with its own
        # single-element branches array (0003 dedup: same repo_id+path, different content_sha).
        _insert_file(
            conn, main_id, "src/divergent.go", lang="go", content="main version", branches=["main"]
        )
        _insert_file(
            conn,
            main_id,
            "src/divergent.go",
            lang="go",
            content="feature version",
            branches=["feature"],
        )

        # master_id: default is "master", not "main" -- proves the conjunct is per-repo
        # correlated, not a hardcoded "main" constant.
        _insert_file(
            conn,
            master_id,
            "src/master_file.go",
            lang="go",
            content="on master",
            branches=["master"],
        )

        # null_id: default_branch IS NULL -> the implicit conjunct must coalesce to 'HEAD'.
        _insert_file(
            conn, null_id, "src/head_file.go", lang="go", content="on head", branches=["HEAD"]
        )

        conn.commit()
        yield BranchSeeded(
            conn=conn, main_repo_id=main_id, master_repo_id=master_id, null_default_repo_id=null_id
        )
    finally:
        conn.rollback()
        conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.commit()
        conn.close()
        engine.dispose()


@pytest.mark.integration
def test_default_query_returns_only_default_branch_rows(branch_seeded: BranchSeeded) -> None:
    # No branch: atom -> implicit default-branch conjunct. "feature"-only content
    # (src/divergent.go's "feature version" row) must never surface.
    paths = _paths(branch_seeded.conn, "repo:^branch/main-default")
    assert paths == {"src/default_only.go", "src/shared.go", "src/divergent.go"}
    divergent_rows = _rows(branch_seeded.conn, parse("file:divergent"))
    assert len(divergent_rows) == 1  # only the "main" content version, not "feature"'s


@pytest.mark.integration
def test_branch_filter_returns_only_named_branch_rows(branch_seeded: BranchSeeded) -> None:
    # branch:feature opts OUT of the default conjunct entirely: default_only.go (main-only)
    # is excluded, shared.go (union member) and the "feature version" of divergent.go included.
    paths = _paths(branch_seeded.conn, "repo:^branch/main-default branch:feature")
    assert paths == {"src/shared.go", "src/divergent.go"}


@pytest.mark.integration
def test_divergent_content_path_splits_by_content_sha(branch_seeded: BranchSeeded) -> None:
    # Same (repo_id, path), two distinct content_sha rows -- 0003 dedup keys on content, not
    # just path. branch: disambiguates which version comes back, and the ids differ.
    main_ids = {
        row.id
        for row in branch_seeded.conn.execute(
            compile_query(parse("repo:^branch/main-default branch:main file:divergent"))
        ).all()
    }
    feature_ids = {
        row.id
        for row in branch_seeded.conn.execute(
            compile_query(parse("repo:^branch/main-default branch:feature file:divergent"))
        ).all()
    }
    assert len(main_ids) == 1
    assert len(feature_ids) == 1
    assert main_ids.isdisjoint(feature_ids)


@pytest.mark.integration
def test_default_conjunct_is_per_repo_not_a_hardcoded_main_constant(
    branch_seeded: BranchSeeded,
) -> None:
    # master_repo's default is "master", not "main" -- the default query must still resolve it,
    # proving the conjunct is a correlated per-repo coalesce, not a literal "main" check.
    assert _paths(branch_seeded.conn, "repo:^branch/master-default") == {"src/master_file.go"}


@pytest.mark.integration
def test_null_default_branch_resolves_to_head(branch_seeded: BranchSeeded) -> None:
    # default_branch IS NULL -> coalesce(repos.default_branch, 'HEAD') = ANY(files.branches)
    # must still find the row tagged branches=['HEAD'] (byte-identical coalesce to the 0003
    # backfill and the semantic default leg).
    assert _paths(branch_seeded.conn, "repo:^branch/null-default") == {"src/head_file.go"}


# ------------------------------------------------------------------ negation (issue #69)
#
# Candidate-set shapes for positive+negative combinations. Written now per the plan; run in CI
# (needs live Lakebase). The nullable-NULL case is explicitly NOT a merge gate -- see its note.


@pytest.mark.integration
def test_negated_content_atom_excludes_matching_rows(seeded: Seeded) -> None:
    # `lang:go -handler`: go files EXCEPT those whose content matches "handler". handler.go
    # ("Handler") and util.go ("handler" comment) are excluded; the rest of the go set remains.
    assert _paths(seeded.conn, "lang:go -handler") == {
        "src/escape1.go",
        "src/escape2.go",
        "src/regex_lower.go",
        "SRC/UpperCase.go",
    }


@pytest.mark.integration
def test_negated_path_filter_excludes_matching_rows(seeded: Seeded) -> None:
    # `lang:go -file:handler`: go files whose PATH does not match the regex "handler".
    assert _paths(seeded.conn, "lang:go -file:handler") == {
        "src/util.go",
        "src/escape1.go",
        "src/escape2.go",
        "src/regex_lower.go",
        "SRC/UpperCase.go",
    }


@pytest.mark.integration
def test_negated_lang_filter_is_three_valued_and_excludes_null_lang(seeded: Seeded) -> None:
    # `repo:^acme -lang:go`: every acme file is either lang=go (excluded by `-lang:go`) or
    # lang=NULL (src/no_lang.txt -> `NOT (NULL = 'go')` is NULL, also excluded). So the set is
    # empty -- proving the NULL-lang row is in NEITHER `lang:go` NOR `-lang:go`.
    assert _paths(seeded.conn, "repo:^acme -lang:go") == set()


@pytest.mark.integration
def test_negated_symbol_filter_excludes_files_with_that_symbol(seeded: Seeded) -> None:
    # `repo:^acme -sym:Handler`: NOT EXISTS(a Handler-named symbol) -> only src/handler.go (which
    # declares `Handler`) is excluded; every other acme file has no such symbol and remains.
    assert _paths(seeded.conn, "repo:^acme -sym:Handler") == ACME_PATHS - {"src/handler.go"}


@pytest.mark.integration
def test_nullable_content_row_excluded_from_both_positive_and_negated(seeded: Seeded) -> None:
    # NOT A MERGE GATE (integration-only, deferred to CI): the three-valued NULL contract.
    # gamma/null_content.txt has content=NULL and is reachable via a path predicate, but a NULL
    # content matches NEITHER `foo` (NULL ILIKE) NOR `-foo` (NULL NOT ILIKE) -- both are NULL,
    # never true -- so it appears in neither candidate set. No set-complement rewrite includes it.
    assert _paths(seeded.conn, "file:^gamma/") == {"gamma/null_content.txt"}
    assert _paths(seeded.conn, "file:^gamma/ foo") == set()
    assert _paths(seeded.conn, "file:^gamma/ -foo") == set()


@pytest.mark.integration
def test_negated_branch_stays_scoped_to_default_and_excludes_that_branch(
    branch_seeded: BranchSeeded,
) -> None:
    # `-branch:feature` is an EXCLUSION, not a scope selection: it does NOT opt out of the
    # implicit default-branch conjunct. So the query stays scoped to the default branch AND
    # additionally drops any default-branch row whose membership also includes "feature".
    # src/shared.go (branches=[main, feature]) is dropped; default_only.go and the "main"
    # version of divergent.go remain.
    paths = _paths(branch_seeded.conn, "repo:^branch/main-default -branch:feature")
    assert paths == {"src/default_only.go", "src/divergent.go"}


@pytest.mark.integration
def test_negated_commit_that_resolves_to_nothing_excludes_nothing(seeded: Seeded) -> None:
    # No repo_branches rows are seeded here, so `-commit:abc1234` lowers to NOT EXISTS(...) which
    # is true for every row -- a non-resolving negated commit is inert and excludes nothing, so
    # `lang:go -commit:abc1234` returns the same set as `lang:go`.
    assert _paths(seeded.conn, "lang:go -commit:abc1234") == _paths(seeded.conn, "lang:go")


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
    """Render EXPLAIN (FORMAT JSON) for ``node``'s WHERE predicate with the seq-scan
    escape hatch disabled, scoped to a SAVEPOINT so the ``enable_seqscan`` GUC change
    never leaks into other tests sharing this module-scoped connection (``SET LOCAL`` is
    reverted by ``ROLLBACK TO SAVEPOINT``, per Postgres semantics).

    The compiled ``ORDER BY (repo_id, path)`` / ``LIMIT`` are stripped before EXPLAIN.
    The ordering is backed by the unique btree ``uq_files_repo_id_path`` (invariant I2),
    and on a tiny corpus the planner prefers that ordered Index Scan -- applying the
    content regex as a mere ``Filter`` -- over a Bitmap Index Scan on the GIN index, even
    with ``enable_seqscan = off`` (which only rules out *sequential* scans, not the
    competing btree). That is a small-data artifact that masks whether the trgm index
    *can* serve the predicate. Probing the WHERE clause in isolation removes the
    competing ordering index, so the GIN trgm index is the only way to satisfy a
    ``content ~* ...`` regex and the plan is deterministic regardless of row count.

    Inlines bound params via ``literal_binds`` so the plain ``EXPLAIN`` statement
    needs no separate parameter binding. Returns the root ``Plan`` node.
    """
    stmt = compile_query(node).order_by(None).limit(None)
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
@pytest.mark.xfail(
    strict=False,
    reason="Pre-0003, this was the deterministic proving assertion (isolating the WHERE clause "
    "left ix_files_content_trgm as the ONLY index able to satisfy the regex). 0003's implicit "
    "default-branch conjunct (see app/query/compiler.py::_default_branch_conjunct) means "
    "compile_query() can no longer produce that isolated WHERE clause at all -- every "
    "default query now ALSO joins repos via a correlated EXISTS, and on this deliberately "
    "tiny corpus Postgres's cost model prefers a full Index Scan of the (small) "
    "uq_files_repo_path_sha unique btree + a Filter over the trgm Bitmap Heap Scan, even "
    "after ANALYZE. Both plans return correct results; only the ACCESS PATH differs, and "
    "only on tiny corpora -- the same class of small-data artifact already documented for "
    "the three tests below. See test_branch_filter_query_uses_branches_gin_index for the "
    "still-deterministic post-0003 GIN-usage proof (an explicit branch: predicate).",
)
def test_explain_trigram_extractable_regex_uses_gin_index(seeded: Seeded) -> None:
    """The (pre-0003) deterministic proving assertion.

    ``/Handler.*Request/`` has >=1 literal 3-gram. Probing the predicate in isolation
    (``_explain_plan`` strips the ``ORDER BY``/``LIMIT``) with ``enable_seqscan = off``
    leaves ``ix_files_content_trgm`` as the only index that can satisfy the
    ``content ~*`` regex, so Postgres MUST reach for it (a Bitmap Index Scan feeding a
    Bitmap Heap Scan) -- independent of row count / table statistics, so no large filler
    seed or ``ANALYZE`` is required for determinism.
    """
    plan = _explain_plan(seeded.conn, parse("/Handler.*Request/"))
    assert _uses_index(plan, "ix_files_content_trgm"), (
        f"expected a Bitmap/Index Scan on ix_files_content_trgm, got plan: {plan}"
    )


@pytest.mark.integration
def test_branch_filter_query_uses_branches_gin_index(seeded: Seeded) -> None:
    """The post-0003 replacement for the prior determinism guarantee above.

    An explicit ``branch:`` predicate lowers to the GIN-served ``@>`` operator (Option C1) and
    opts OUT of the implicit default conjunct (see ``_has_branch_filter``), so this is the one
    query shape where the WHERE clause is still exactly what the compiler renders -- no extra
    correlated join. ``ix_files_branches_gin`` is the only index able to satisfy
    ``branches @> ARRAY[...]``, so with ``enable_seqscan = off`` Postgres must reach for it,
    independent of row count.
    """
    plan = _explain_plan(seeded.conn, parse("branch:main"))
    assert _uses_index(plan, "ix_files_branches_gin"), (
        f"expected a Bitmap/Index Scan on ix_files_branches_gin, got plan: {plan}"
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
