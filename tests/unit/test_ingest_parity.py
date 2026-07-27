"""Oracle parity: ``indexer.ingest`` must agree with ``indexer.parse``, file for file.

``indexer.parse.iter_source_files`` has no production caller after #106. It is kept
deliberately, as the **executable specification** the streaming path is pinned
against: it cannot be deleted without editing ``indexer/parse.py``, which
``tests/unit/test_semantics_version_tripwire.py`` watches and which would force
the ``INDEX_SEMANTICS_VERSION`` bump #106's AC3 forbids.

The expected value here is computed LIVE from ``parse.py`` -- each fixture tarball
is extracted to disk exactly the way ``extract_tarball`` used to extract it
(``filter="data"``), walked with ``iter_source_files``, and compared against
``iter_tar_source_files`` over the same tarball. A golden-fixture version of this
test could be regenerated to match a drifting ``ingest.py``; this one cannot. Do
not substitute one.

**Fixture constraint, load-bearing:** every member here must be one
``filter="data"`` survives. The oracle side raises on special files and on
absolute or escaping links (probe-verified), so including them would make this
test red by construction rather than by defect. Those member shapes are pinned in
``test_ingest.py`` instead.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from indexer.hashing import content_sha
from indexer.ingest import iter_tar_source_files
from indexer.languages import EXT_TO_LANG, MAX_FILE_BYTES, ParsedFile
from indexer.parse import iter_source_files
from tests.unit.test_ingest import TOP, _dir, _Entry, _reg, _sym, _write

# A NUL past the 8 KB sniff window: legal UTF-8, decodes cleanly, and is stripped
# before the yield -- so `size` (the archive-declared length) exceeds len(content).
_NUL_PAST_SNIFF = b"a" * 9000 + b"\x00" + b"b"

_RICH_FIXTURE: list[_Entry] = [
    _dir(TOP),
    _reg(f"{TOP}/README.md", b"# hello\n"),
    _reg(f"{TOP}/empty.py", b""),
    _reg(f"{TOP}/Makefile", b"all:\n"),
    _reg(f"{TOP}/deeply/nested/dir/mod.py", b"CONST = 1\n"),
    _reg(f"{TOP}/café.py", b"# non-ascii filename\n"),
    _dir(f"{TOP}/emptydir"),
    _reg(f"{TOP}/real.py", b"def f():\n    return 1\n"),
    _sym(f"{TOP}/link.py", "real.py"),
    _reg(f"{TOP}/.git/config", b"[core]\n"),
    _reg(f"{TOP}/huge.py", b"x" * (MAX_FILE_BYTES + 1)),
    _reg(f"{TOP}/binary.png", b"\x89PNG\x00\x00 not text"),
    _reg(f"{TOP}/nul_past_sniff.py", _NUL_PAST_SNIFF),
    _reg(f"{TOP}/latin1.txt", b"\xff\xfe caf\xe9"),
    # Uppercase suffix: both sides must lower-case before the EXT_TO_LANG lookup
    # (`parse.py` uses `entry.suffix.lower()`, `ingest.py` the relative path's).
    # Without this entry nothing in the suite would notice a dropped `.lower()`.
    _reg(f"{TOP}/UPPER.PY", b"UPPER = 1\n"),
] + [_reg(f"{TOP}/sample{ext}", b"// sample\n") for ext in sorted(EXT_TO_LANG)]

# Only members BOTH sides skip: `.git/`, oversized, NUL-sniffed binary, invalid
# UTF-8, a directory, and a benign internal symlink.
_UNINDEXABLE_FIXTURE: list[_Entry] = [
    _dir(TOP),
    _dir(f"{TOP}/sub"),
    _reg(f"{TOP}/.git/config", b"[core]\n"),
    _reg(f"{TOP}/huge.py", b"x" * (MAX_FILE_BYTES + 1)),
    _reg(f"{TOP}/binary.png", b"\x89PNG\x00\x00 not text"),
    _reg(f"{TOP}/latin1.txt", b"\xff\xfe caf\xe9"),
    _sym(f"{TOP}/link", ".git/config"),
]


def _oracle(tar_path: Path, dest: Path) -> list[ParsedFile]:
    """Extract exactly as ``extract_tarball`` did, then walk with ``parse.py``."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, mode="r:*") as tf:
        tf.extractall(dest, filter="data")
    return list(iter_source_files(dest / TOP))


def _key(pf: ParsedFile) -> tuple[str, str | None, int, str, str]:
    return (pf.path, pf.lang, pf.size, pf.content, content_sha(pf.content))


@pytest.mark.unit
def test_parity_with_iter_source_files(tmp_path: Path) -> None:
    """Set equality on every field the corpus is built from, over a rich fixture.

    Set equality, not sequence equality: the stream yields in archive order (D5),
    which is deterministic per commit but is not ``sorted()``. Nothing downstream
    of ``index_repo`` depends on the order.
    """
    tar_path = _write(tmp_path, _RICH_FIXTURE)

    expected = _oracle(tar_path, tmp_path / "extracted")
    actual = list(iter_tar_source_files(tar_path))

    assert expected, "the fixture must actually index something"
    assert {_key(pf) for pf in actual} == {_key(pf) for pf in expected}


@pytest.mark.unit
def test_parity_holds_when_no_file_is_indexable(tmp_path: Path) -> None:
    """Both sides must agree on the EMPTY result too, not just on a populated one.

    An archive of nothing but skips is the shape most likely to expose a filter
    that fires on one side and not the other.
    """
    tar_path = _write(tmp_path, _UNINDEXABLE_FIXTURE)

    assert _oracle(tar_path, tmp_path / "extracted") == []
    assert list(iter_tar_source_files(tar_path)) == []


@pytest.mark.unit
def test_lang_and_size_match_parse_exactly_for_every_fixture_file(tmp_path: Path) -> None:
    """D11: ``lang`` and ``size``, asserted field-by-field and on purpose.

    The set-equality test above already covers these, so this looks redundant --
    it is not, and the reason must be said out loud. #104's delta gate classifies
    a file on ``(path, content_sha)`` ALONE (``indexer/store.py``). If
    ``ingest.py`` ever derived ``size`` or ``lang`` differently from ``parse.py``,
    every already-stored file would classify as *unchanged*, no ``UPDATE`` would
    be issued, and the stored columns would never be corrected: content parity
    would hold perfectly while ``lang``/``size`` rotted silently and permanently.
    ``store.py`` reasons it is protected from that by the tripwire watching the
    modules that derive them -- and #106 moved that derivation into ``ingest.py``.
    So: ``size`` is ``member.size`` (the archive-declared length, which exceeds
    ``len(content)`` after NUL stripping), and ``lang`` comes from the RELATIVE
    path's lowered suffix, after the top directory is stripped.
    """
    tar_path = _write(tmp_path, _RICH_FIXTURE)

    expected = {pf.path: pf for pf in _oracle(tar_path, tmp_path / "extracted")}
    actual = {pf.path: pf for pf in iter_tar_source_files(tar_path)}

    assert set(actual) == set(expected)
    for path, pf in actual.items():
        assert pf.lang == expected[path].lang, f"lang diverged for {path}"
        assert pf.size == expected[path].size, f"size diverged for {path}"

    # The fixture really does exercise both edges this test exists for.
    assert actual["nul_past_sniff.py"].size > len(actual["nul_past_sniff.py"].content)
    assert {pf.lang for pf in actual.values()} >= set(EXT_TO_LANG.values())
