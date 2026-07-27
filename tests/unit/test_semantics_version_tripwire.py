"""CI tripwire: extraction-semantics changes must bump ``INDEX_SEMANTICS_VERSION``.

The version mechanism is fully automatic *once bumped* -- a repo whose stored
``(last_indexed_commit, index_semantics_version)`` no longer matches is
re-indexed on the next run without any operator action. But the bump itself has
no enforcement: change how symbols are extracted, forget the constant, and every
already-indexed repo keeps serving results produced by the OLD extractor, at
HEAD, looking perfectly current. That failure is silent and open-ended.

So the obligation is made unforgettable rather than optional: if a diff touches
any module that decides WHAT gets extracted, the same diff must change the
``INDEX_SEMANTICS_VERSION`` line. This follows the source-level-check precedent
of ``test_ci_branch.py`` and ``test_migration_source.py`` -- no database, no
network, just ``git``.

Deliberately skipped rather than failed when git or the base ref is unavailable
(shallow clones, tarball checkouts, a worktree with no remote): a tripwire that
fails spuriously on a developer laptop gets disabled, and a disabled tripwire
guards nothing.

``indexer/ingest.py`` (#106) joined the watch set once it became the
production file source -- streaming tarball ingestion replaced
``indexer/parse.py``'s ``iter_source_files`` as the caller of the extraction
filter chain, and ``ingest.py`` also owns the ``lang``/``size`` derivation
that ``indexer.store``'s delta gate relies on the tripwire to protect
(``indexer/store.py``, the "two columns are deliberately NOT re-derived"
note). Leaving it unwatched would point that part of the guard at dead code.
``indexer/parse.py`` stays watched too: it still owns the production chunker
(``iter_chunks``, called from ``indexer/job.py``) and the shared binary-sniff
helper ``ingest.py`` imports, on top of its retained role as ``ingest.py``'s
executable oracle -- only ``iter_source_files`` lost its production caller.

**Expected false positive, not confined to a single future event.** Adding a
new path to ``SEMANTICS_PATHS`` makes it an offender in any diff whose base
predates the file's creation -- ``git diff --name-only`` lists ADDED files,
not just changed ones. ``indexer/ingest.py`` postdates ``master`` (it was
added by #106's first PR, which has not merged past
``integration/indexer-performance``), so **every local run of this test on
this branch or its descendants sees it as an offender today** -- not only
the future diff that folds ``integration/indexer-performance`` into
``master`` (#111). ``_base_ref()`` falls back to ``origin/master`` locally
(there is usually no ``GITHUB_BASE_REF``), so this is the common case, not
the rare one; CI is actually less likely to see it, since both workflows
checkout at the default depth-1 and this test typically skips there instead
of running. That is expected, not a regression, and not a reason to disable
the test: resolve it via this tripwire's own documented escape below ("if
this change genuinely cannot alter extraction output, say so in the PR AND
ADJUST SEMANTICS_PATHS") -- the "say so in the PR" half only. Do NOT bump
``INDEX_SEMANTICS_VERSION`` and do NOT remove ``indexer/ingest.py`` from
``SEMANTICS_PATHS`` to silence it; the parity test
(``tests/unit/test_ingest_parity.py``, which pins ``ingest.py`` against
``parse.py`` field-for-field on the ``(path, lang, size, content)`` set) is
what actually backs the "no output change" claim.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Modules that decide WHAT ends up in the index. A change to any of them can
# alter extraction output for an unchanged commit, which is exactly the
# condition the stored version stamp exists to detect.
#
# ``indexer/parse.py``'s ``iter_source_files`` keeps no production caller
# after #106, but the module stays watched: it still owns the production
# chunker (``iter_chunks``) and the shared binary sniff, on top of being
# ``indexer/ingest.py``'s executable oracle. See the module docstring above
# for why ``indexer/ingest.py`` joined this set and for the expected
# same-branch false positive.
SEMANTICS_PATHS = (
    "indexer/symbols.py",
    "indexer/parse.py",
    "indexer/languages.py",
    "indexer/ingest.py",
)

# The constant lives in app/db/models.py (it is read by both indexer.store, which
# writes the stamp, and indexer.job, which compares it), NOT in indexer/store.py.
VERSION_FILE = "app/db/models.py"
VERSION_CONST = "INDEX_SEMANTICS_VERSION"


def _git(*args: str) -> str | None:
    """Run a git command in the repo, returning stdout or ``None`` if it fails."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _base_ref() -> str:
    """The ref this branch is diffed against.

    ``GITHUB_BASE_REF`` is set on pull-request workflows; local runs fall back to
    the default branch. Whichever it is, it must actually resolve -- an
    unresolvable ref skips rather than fails.
    """
    github_base = os.environ.get("GITHUB_BASE_REF")
    candidates = [f"origin/{github_base}"] if github_base else []
    candidates += ["origin/master", "master", "origin/main", "main"]
    for ref in candidates:
        if _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            return ref
    pytest.skip("no base ref available (shallow clone or detached checkout)")


def semantics_touched(changed_paths: set[str]) -> list[str]:
    """The watched extraction modules present in ``changed_paths``. Pure."""
    return sorted(changed_paths.intersection(SEMANTICS_PATHS))


def version_was_bumped(version_diff: str) -> bool:
    """True iff ``version_diff`` adds or removes an ``INDEX_SEMANTICS_VERSION`` line.

    Pure, so the tripwire's decision can be tested against synthetic diffs
    rather than only against whatever this branch happens to contain.
    """
    return any(
        line.startswith(("+", "-")) and VERSION_CONST in line
        for line in version_diff.splitlines()
        # Skip the +++/--- file headers, which name the path, not a change.
        if not line.startswith(("+++", "---"))
    )


@pytest.mark.unit
def test_semantics_change_bumps_the_index_semantics_version() -> None:
    if _git("rev-parse", "--git-dir") is None:
        pytest.skip("not a git checkout")
    base = _base_ref()

    changed = _git("diff", "--name-only", f"{base}...HEAD")
    if changed is None:
        pytest.skip(f"could not diff against {base}")
    touched = {line.strip() for line in changed.splitlines() if line.strip()}

    offenders = semantics_touched(touched)
    if not offenders:
        return

    version_diff = _git("diff", "-U0", f"{base}...HEAD", "--", VERSION_FILE) or ""
    assert version_was_bumped(version_diff), (
        f"this branch changes extraction semantics ({', '.join(offenders)}) "
        f"but does not change {VERSION_CONST} in {VERSION_FILE}. Without a bump, "
        "every already-indexed repo keeps serving output from the OLD extractor "
        "and never re-indexes, because its stored stamp still matches HEAD. "
        f"Bump {VERSION_CONST}, or -- if this change genuinely cannot alter "
        "extraction output -- say so in the PR and adjust SEMANTICS_PATHS."
    )


@pytest.mark.unit
def test_tripwire_watches_the_file_that_actually_holds_the_constant() -> None:
    """Guard the guard: a moved constant must not silently disarm the check.

    ``INDEX_SEMANTICS_VERSION`` has already moved once (indexer/store.py ->
    app/db/models.py). If it moves again, the diff check above would look at an
    unrelated file and pass unconditionally -- a tripwire that is still green
    while guarding nothing. This fails loudly instead.
    """
    text = (_REPO_ROOT / VERSION_FILE).read_text()
    assert f"{VERSION_CONST} = " in text, (
        f"{VERSION_CONST} is no longer defined in {VERSION_FILE}; update VERSION_FILE "
        "here or the semantics tripwire silently stops guarding anything"
    )


@pytest.mark.unit
def test_watched_semantics_paths_all_exist() -> None:
    """A renamed module would otherwise drop out of the watch set unnoticed."""
    for rel in SEMANTICS_PATHS:
        assert (_REPO_ROOT / rel).is_file(), (
            f"{rel} is watched by the semantics tripwire but does not exist; "
            "update SEMANTICS_PATHS to match the rename"
        )


# --- the decision itself, against synthetic diffs ---------------------------
# Asserted directly rather than only through whatever this branch happens to
# contain: a tripwire whose firing condition is never exercised is a tripwire
# nobody has evidence works.


@pytest.mark.unit
def test_touching_symbols_alone_is_a_semantics_change() -> None:
    assert semantics_touched({"indexer/symbols.py", "README.md"}) == ["indexer/symbols.py"]


@pytest.mark.unit
def test_unrelated_paths_are_not_a_semantics_change() -> None:
    assert semantics_touched({"README.md", "app/main.py", "indexer/store.py"}) == []


@pytest.mark.unit
def test_an_empty_version_diff_is_not_a_bump() -> None:
    """The firing case: symbols.py changed, the constant untouched."""
    assert version_was_bumped("") is False


@pytest.mark.unit
def test_a_changed_constant_line_counts_as_a_bump() -> None:
    diff = (
        "diff --git a/app/db/models.py b/app/db/models.py\n"
        "--- a/app/db/models.py\n"
        "+++ b/app/db/models.py\n"
        "@@ -17 +17 @@\n"
        f"-{VERSION_CONST} = 1\n"
        f"+{VERSION_CONST} = 2\n"
    )
    assert version_was_bumped(diff) is True


@pytest.mark.unit
def test_an_unrelated_edit_to_the_version_file_is_not_a_bump() -> None:
    """Editing models.py at all must not satisfy the tripwire — only the constant does."""
    diff = (
        "--- a/app/db/models.py\n"
        "+++ b/app/db/models.py\n"
        "@@ -40 +40 @@\n"
        '-    """Old docstring."""\n'
        '+    """New docstring."""\n'
    )
    assert version_was_bumped(diff) is False


@pytest.mark.unit
def test_file_headers_naming_the_path_do_not_count_as_a_bump() -> None:
    """A ``+++``/``---`` header carries the filename, not a changed line.

    If the constant ever moves into a path containing its own name, a naive
    prefix check would read those headers as a bump and pass unconditionally.
    """
    diff = f"--- a/{VERSION_CONST}.py\n+++ b/{VERSION_CONST}.py\n"
    assert version_was_bumped(diff) is False
