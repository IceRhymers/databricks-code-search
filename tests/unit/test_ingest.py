"""Unit tests for indexer.ingest.iter_tar_source_files against in-memory tarballs.

The behavioural-divergence tests below carry the OLD (extract-to-disk) behaviour in
their docstrings. Those are probe-verified against ``indexer.fetch.extract_tarball``
+ ``indexer.parse.iter_source_files`` on Python 3.12, not inferred from the
``tarfile`` documentation, which misleads on several of them -- four rows that
sound like they must have raised in fact silently dropped or silently INDEXED a
member. Do not "correct" them from memory.

Whole-corpus equivalence with ``indexer.parse.iter_source_files`` lives in
``test_ingest_parity.py``; this file pins the edges that parity cannot reach
(members ``filter="data"`` rejects outright).
"""

from __future__ import annotations

import io
import logging
import tarfile
from pathlib import Path
from typing import Any

import pytest

import indexer.ingest as ingest
from indexer.ingest import iter_tar_source_files
from indexer.languages import MAX_FILE_BYTES

ORG = "acme"
REPO = "widgets"
SHA = "abc1234def5678"
TOP = f"{ORG}-{REPO}-{SHA[:7]}"

_Entry = tuple[tarfile.TarInfo, bytes | None]


# --- fixture builders (mirrors tests/unit/test_fetch.py's, plus link/dir/special) ---


def _reg(name: str, data: bytes) -> _Entry:
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.size = len(data)
    return info, data


def _dir(name: str) -> _Entry:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info, None


def _sym(name: str, target: str) -> _Entry:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info, None


def _lnk(name: str, target: str) -> _Entry:
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE
    info.linkname = target
    return info, None


def _fifo(name: str) -> _Entry:
    info = tarfile.TarInfo(name)
    info.type = tarfile.FIFOTYPE
    return info, None


def _make_tarball(entries: list[_Entry]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for info, data in entries:
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def _write(tmp_path: Path, entries: list[_Entry], name: str = "source.tar.gz") -> Path:
    out = tmp_path / name
    out.write_bytes(_make_tarball(entries))
    return out


_CLEAN = [
    _dir(TOP),
    _reg(f"{TOP}/README.md", b"# hello\n"),
    _reg(f"{TOP}/src/main.py", b"def f():\n    return 1\n"),
]


class _CountingBytesIO(io.BytesIO):
    """A seekable in-memory file that records how many bytes were pulled out of it."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.bytes_read = 0

    def read(self, size: int | None = -1, /) -> bytes:
        chunk = super().read(size)
        self.bytes_read += len(chunk)
        return chunk

    def read1(self, size: int = -1, /) -> bytes:
        chunk = super().read1(size)
        self.bytes_read += len(chunk)
        return chunk


def _capture_tarfiles(monkeypatch: pytest.MonkeyPatch) -> list[tarfile.TarFile]:
    """Record every ``TarFile`` ``iter_tar_source_files`` opens.

    ``tf`` is local to the generator, so this monkeypatch on
    ``indexer.ingest.tarfile.open`` is the only handle a test can get on it.
    """
    opened: list[tarfile.TarFile] = []
    real_open = tarfile.open

    def _spy(*args: Any, **kwargs: Any) -> tarfile.TarFile:
        tf = real_open(*args, **kwargs)
        opened.append(tf)
        return tf

    monkeypatch.setattr(ingest.tarfile, "open", _spy)
    return opened


# --- AC1 / AC2: one decompression, nothing on disk --------------------------


@pytest.mark.unit
def test_decompresses_the_archive_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1. Measures DECOMPRESSED BYTES PULLED, not ``getmembers()`` calls.

    A spy on ``getmembers`` would pass for an implementation that opens the
    archive twice and streams it twice -- exactly the double decompression this
    change exists to remove. ``ingest`` calls ``tarfile.open`` with a path, so
    there is no file object to wrap: patch ``open`` itself and re-dispatch onto a
    counting ``fileobj``.

    The 1.05x threshold is safe rather than sloppy: ``mode="r:gz"`` dispatches
    straight to ``gzopen`` with no codec probing (so no failed-probe re-read), and
    ``next()`` never seeks backwards, so no ``DecompressReader.rewind()`` occurs.
    The end-of-stream drain reads only the trailing block padding. A second pass
    would double the count outright.
    """
    tar_path = _write(tmp_path, _CLEAN)
    compressed = tar_path.read_bytes()
    reader = _CountingBytesIO(compressed)
    opens: list[tuple[Any, ...]] = []
    real_open = tarfile.open

    def _spy(*args: Any, **kwargs: Any) -> tarfile.TarFile:
        opens.append(args)
        return real_open(fileobj=reader, mode="r:gz")

    monkeypatch.setattr(ingest.tarfile, "open", _spy)

    files = list(iter_tar_source_files(tar_path))

    assert [pf.path for pf in files] == ["README.md", "src/main.py"]
    assert len(opens) == 1, "the archive was opened more than once"
    assert reader.bytes_read <= len(compressed) * 1.05, (
        f"read {reader.bytes_read} bytes from a {len(compressed)}-byte archive; "
        "that is a second pass"
    )


# --- happy path -------------------------------------------------------------


@pytest.mark.unit
def test_yields_expected_files_from_a_clean_tarball(tmp_path: Path) -> None:
    """Top dir stripped, extensions mapped, unknown extensions kept with lang=None."""
    tar_path = _write(
        tmp_path,
        [
            _dir(TOP),
            _reg(f"{TOP}/README.md", b"# hello\n"),
            _reg(f"{TOP}/src/main.py", b"def f():\n    return 1\n"),
            _reg(f"{TOP}/Makefile", b"all:\n"),
        ],
    )
    by_path = {pf.path: pf for pf in iter_tar_source_files(tar_path)}

    assert set(by_path) == {"README.md", "src/main.py", "Makefile"}
    assert by_path["src/main.py"].lang == "python"
    assert by_path["src/main.py"].content == "def f():\n    return 1\n"
    assert by_path["src/main.py"].size == len(b"def f():\n    return 1\n")
    assert by_path["README.md"].lang is None
    assert by_path["Makefile"].lang is None


@pytest.mark.unit
def test_ordering_is_deterministic(tmp_path: Path) -> None:
    """D5: archive order, and archive order is fixed for a fixed commit.

    The contract downstream is determinism plus set-equality, never a sorted
    sequence -- sorting would need either a metadata pass (the decompression AC1
    removes) or the whole corpus in memory.
    """
    entries = [_dir(TOP)] + [_reg(f"{TOP}/{n}.py", b"x = 1\n") for n in ("z", "a", "m", "b")]
    tar_path = _write(tmp_path, entries)

    first = [pf.path for pf in iter_tar_source_files(tar_path)]
    second = [pf.path for pf in iter_tar_source_files(tar_path)]

    assert first == second
    assert first == ["z.py", "a.py", "m.py", "b.py"]


@pytest.mark.unit
def test_size_is_the_archive_declared_length_not_len_content(tmp_path: Path) -> None:
    """``size`` is ``member.size`` -- parse.py's ``stat().st_size`` analogue.

    A NUL past the 8 KB sniff window is legal UTF-8, decodes cleanly, and is then
    stripped (Postgres ``text`` rejects it), so the stored content is SHORTER than
    the file. Using ``len(content)`` here would diverge from ``parse.py`` on every
    such file -- and #104's delta gate keys only on ``(path, content_sha)``, so
    that divergence would classify as *unchanged* and never be corrected.
    """
    data = b"a" * 9000 + b"\x00" + b"b"
    tar_path = _write(tmp_path, [_dir(TOP), _reg(f"{TOP}/nul.py", data)])

    (pf,) = list(iter_tar_source_files(tar_path))
    assert pf.size == len(data)
    assert pf.content == "a" * 9000 + "b"
    assert pf.size > len(pf.content)


# --- the caps ---------------------------------------------------------------


@pytest.mark.unit
def test_oversized_member_is_skipped_without_reading_its_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D9: the size check is on the header, so a huge blob's data is never read.

    ``member.size`` replaces ``entry.stat().st_size`` for exactly this reason --
    peak memory stays bounded by ``MAX_FILE_BYTES``, not by the largest member.
    """
    extracted: list[str] = []
    real_extractfile = tarfile.TarFile.extractfile

    def _spy(self: tarfile.TarFile, member: Any) -> Any:
        extracted.append(member.name)
        return real_extractfile(self, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", _spy)

    tar_path = _write(
        tmp_path,
        [
            _dir(TOP),
            _reg(f"{TOP}/huge.py", b"x" * (MAX_FILE_BYTES + 1)),
            _reg(f"{TOP}/small.py", b"x = 1\n"),
        ],
    )
    assert [pf.path for pf in iter_tar_source_files(tar_path)] == ["small.py"]
    assert extracted == [f"{TOP}/small.py"]


@pytest.mark.unit
def test_incremental_cap_raises_at_the_same_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D9: same threshold as the old up-front ``sum(m.size ...)``, different timing.

    The single member sits at offset 0 so the coarser ``member.offset`` guard
    cannot fire first -- this test is specifically about the regular-file size
    accumulator, which stays the tighter of the two for one oversized blob. The
    message carries the actual magnitude, not just the cap, so an operator can
    tell "repo slightly over" from "gzip bomb".
    """
    monkeypatch.setattr(ingest, "MAX_EXTRACTED_BYTES", 4)
    tar_path = _write(tmp_path, [_reg(f"{TOP}/big.py", b"0123456789")])

    with pytest.raises(ValueError, match="streams to 10 bytes of content, exceeding 4"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_cap_counts_filtered_members_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D9: pre-filter accounting, matching ``sum(m.size for m in members if m.isreg())``.

    The old bomb check ran before (and independently of) the ``.git``/size/binary
    filters. An archive whose bulk is ``.git/`` objects is still a bomb, so the
    counter is incremented before any of them. The blob is the FIRST member, at
    offset 0, so this pins the size accumulator rather than the coarser
    ``member.offset`` guard.
    """
    monkeypatch.setattr(ingest, "MAX_EXTRACTED_BYTES", 4)
    tar_path = _write(tmp_path, [_reg(f"{TOP}/.git/objects/pack/blob", b"0123456789")])

    with pytest.raises(ValueError, match="streams to 10 bytes of content, exceeding 4"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_offset_cap_bounds_members_that_carry_no_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regular-file accumulator alone does NOT bound decompression.

    Directory, link and special members carry no data, so ``streamed`` never
    moves for them -- an archive of a million such headers would decompress
    unbounded past a cap that only ever sums ``member.size`` for ``isreg()``
    members. ``member.offset`` (the tar stream's cumulative decompressed
    position) is checked BEFORE the ``isreg()`` gate for exactly this reason.
    """
    monkeypatch.setattr(ingest, "MAX_EXTRACTED_BYTES", 1024)
    entries: list[_Entry] = [_dir(TOP)] + [_dir(f"{TOP}/d{n}") for n in range(8)]
    tar_path = _write(tmp_path, entries)

    with pytest.raises(ValueError, match="decompressed bytes at member"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
@pytest.mark.parametrize("keep", [0.3, 0.6, 0.9])
def test_truncated_archive_raises_instead_of_yielding_a_partial_set(
    tmp_path: Path, keep: float
) -> None:
    """A cut-short download must fail the branch, never index a prefix of it.

    ``TarFile.next()`` re-raises a truncated header only when ``self.offset ==
    0``; past the first member it can simply return ``None`` and end the loop.
    That is worse than yielding nothing: the files beyond the cut are absent from
    ``index_repo``'s seen set, so the membership sweep DELETES them from the
    corpus, and the branch is then stamped as current at a HEAD it only partly
    read. Draining the remaining bytes after the loop forces gzip's CRC32/ISIZE
    trailer check, which a truncated stream cannot pass.
    """
    entries: list[_Entry] = [_dir(TOP)] + [
        _reg(f"{TOP}/f{n}.py", (f"def f{n}():\n    return {n}\n" * 40).encode()) for n in range(60)
    ]
    tar_path = _write(tmp_path, entries)
    assert len(list(iter_tar_source_files(tar_path))) == 60  # intact: all present

    cut = tmp_path / "cut.tar.gz"
    cut.write_bytes(tar_path.read_bytes()[: int(tar_path.stat().st_size * keep)])

    with pytest.raises((EOFError, tarfile.ReadError, ValueError)):
        list(iter_tar_source_files(cut))


@pytest.mark.unit
def test_members_list_does_not_grow_across_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6/D9: ``tf.members.clear()`` is the FIRST statement of the loop body.

    ``TarFile.next()`` appends every ``TarInfo`` to ``self.members``, and unlike
    ``extract_tarball`` (whose list died before ``engine.connect()``) this
    ``TarFile`` lives for the whole open transaction. At the END of the loop body
    the clear would be skipped by every ``continue`` -- which is most members in a
    real repo -- and the list would grow unbounded.
    """
    entries: list[_Entry] = [_dir(TOP)]
    for n in range(30):
        entries.append(_reg(f"{TOP}/.git/obj{n}", b"skipped\n"))
        entries.append(_reg(f"{TOP}/f{n}.py", b"x = 1\n"))
    # Built BEFORE the spy is installed -- `_write` opens a TarFile of its own.
    tar_path = _write(tmp_path, entries)
    opened = _capture_tarfiles(monkeypatch)

    for _pf in iter_tar_source_files(tar_path):
        assert len(opened) == 1
        assert len(opened[0].members) <= 1

    assert len(opened[0].members) <= 1


# --- D7: member names -------------------------------------------------------


@pytest.mark.unit
def test_absolute_member_name_raises_where_today_it_was_silently_dropped(
    tmp_path: Path,
) -> None:
    """PROBE-VERIFIED old behaviour: NO error.

    ``filter="data"`` stripped the leading ``/`` and extracted ``/etc/passwd`` to
    ``dest/etc/passwd``; ``fetch.py``'s top-level scan then excluded the member
    (it started with ``/``), so the file landed outside ``root`` and was SILENTLY
    IGNORED. Now it fails the branch, naming the member.
    """
    tar_path = _write(
        tmp_path, [_dir(TOP), _reg(f"{TOP}/ok.py", b"x = 1\n"), _reg("/etc/passwd", b"root\n")]
    )

    with pytest.raises(ValueError, match="absolute name"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_absolute_member_name_under_the_top_dir_raises(tmp_path: Path) -> None:
    """PROBE-VERIFIED old behaviour: NO error, and ``evil.py`` was INDEXED.

    The POSIX filter stripped the leading ``/`` from ``/{TOP}/evil.py``, which
    landed it *inside* ``root`` under a path the archive never actually declared.
    This is the latent path-confusion bug the raise closes.
    """
    tar_path = _write(tmp_path, [_dir(TOP), _reg(f"/{TOP}/evil.py", b"pwned = 1\n")])

    with pytest.raises(ValueError, match="absolute name"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_parent_traversal_member_name_raises(tmp_path: Path) -> None:
    """Old behaviour: ``tarfile.OutsideDestinationError``, branch failed. Now ValueError."""
    tar_path = _write(tmp_path, [_dir(TOP), _reg("../evil.txt", b"pwned\n")])

    with pytest.raises(ValueError, match=r"'\.\.' component"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_parent_traversal_inside_the_destination_raises(tmp_path: Path) -> None:
    """PROBE-VERIFIED old behaviour: NO error, SILENTLY IGNORED.

    ``{TOP}/../evil.txt`` normalises to ``evil.txt``, which is inside ``dest`` --
    so ``filter="data"`` passed it -- but outside ``root``, so the walk never saw
    it. The check here is on the RAW components precisely so this row raises:
    normalising first would return ``evil.txt`` and allow it.
    """
    tar_path = _write(tmp_path, [_dir(TOP), _reg(f"{TOP}/../evil.txt", b"pwned\n")])

    with pytest.raises(ValueError, match=r"'\.\.' component"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_parent_traversal_back_under_the_top_dir_raises(tmp_path: Path) -> None:
    """PROBE-VERIFIED old behaviour: NO error, and ``evil.py`` was INDEXED.

    ``{TOP}/../{TOP}/evil.py`` normalises back to ``{TOP}/evil.py``. This is the
    dangerous row: a ``posixpath.normpath``-then-check implementation would
    ALLOW it, and it would then pass the top-dir check and be indexed as
    ``evil.py`` -- silently reproducing the exact bug this change claims to close.
    """
    tar_path = _write(tmp_path, [_dir(TOP), _reg(f"{TOP}/../{TOP}/evil.py", b"pwned = 1\n")])

    with pytest.raises(ValueError, match=r"'\.\.' component"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_empty_member_name_raises(tmp_path: Path) -> None:
    """Old behaviour: ``IsADirectoryError`` out of ``extractall``. Now a named ValueError.

    ``posixpath.normpath("")`` returns ``"."``, which is why the empty name needs
    its own check rather than falling out of the traversal one.
    """
    tar_path = _write(tmp_path, [_reg("", b"")])

    with pytest.raises(ValueError, match="empty name"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_member_named_exactly_the_top_dir_raises(tmp_path: Path) -> None:
    """A REGULAR file named exactly ``{TOP}`` leaves an empty relative path.

    Old behaviour: ``rglob`` on a non-directory yielded nothing, so the branch
    indexed zero files. Streaming would otherwise yield ``path=""``.
    """
    tar_path = _write(tmp_path, [_reg(TOP, b"not a directory\n")])

    with pytest.raises(ValueError, match="top-level dir itself"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_two_top_level_dirs_raise(tmp_path: Path) -> None:
    """GitHub tarballs carry exactly one ``org-repo-<sha7>/``; anything else is not one."""
    tar_path = _write(
        tmp_path,
        [_dir(TOP), _reg(f"{TOP}/a.py", b"x = 1\n"), _reg("other/b.py", b"y = 2\n")],
    )

    with pytest.raises(ValueError, match="exactly one top-level dir"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_empty_archive_raises(tmp_path: Path) -> None:
    """The one bug that would be INVISIBLE in production, so it gets an explicit raise.

    Without it a truncated or empty archive yields zero files, ``index_repo``
    skips its sweep on the empty-seen-set guard, and ``_stamp_repo_branch(
    seen_any=False)`` writes ``(head_sha, unchanged version)``. For a branch
    already stamped at the current ``INDEX_SEMANTICS_VERSION`` -- the production
    steady state -- that matches the skip seam exactly, silently marking the
    branch current at a HEAD whose corpus was never read, until HEAD moves again.
    #104's ``seen_any`` guard narrows this to that case; it does NOT remove it.
    """
    tar_path = _write(tmp_path, [])

    with pytest.raises(ValueError, match="found none"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_leading_dot_slash_is_normalised(tmp_path: Path) -> None:
    """PROBE-VERIFIED old behaviour: the top dir was NOT stripped.

    ``./{TOP}/a.py`` extracted to ``dest/{TOP}/a.py``, but ``fetch.py``'s
    top-level scan saw ``.`` as the single top-level name and returned ``dest``
    itself as ``root`` -- so every path came out prefixed with ``{TOP}/``.
    """
    tar_path = _write(tmp_path, [_dir(f"./{TOP}"), _reg(f"./{TOP}/a.py", b"x = 1\n")])

    assert [pf.path for pf in iter_tar_source_files(tar_path)] == ["a.py"]


# --- D7: link targets (fail-loud posture preserved) -------------------------


@pytest.mark.unit
def test_symlink_with_an_absolute_target_raises(tmp_path: Path) -> None:
    """Preserves today's ``tarfile.AbsoluteLinkError``: the branch still fails.

    Nothing is written to disk any more, so a skip would be safe -- but changing
    which repos index is out of #106's scope. Relaxing it is a separate issue.
    """
    tar_path = _write(tmp_path, [_dir(TOP), _sym(f"{TOP}/link.py", "/etc/passwd")])

    with pytest.raises(ValueError, match="absolute target"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_symlink_escaping_the_archive_raises(tmp_path: Path) -> None:
    """Preserves today's ``tarfile.LinkOutsideDestinationError``.

    The check is pure ``posixpath`` string arithmetic. A transliteration of
    ``filter="data"``'s ``os.path.realpath`` would resolve against the real
    filesystem and the process cwd -- silently wrong, not merely strict.
    """
    tar_path = _write(tmp_path, [_dir(TOP), _sym(f"{TOP}/link.py", "../../../etc/passwd")])

    with pytest.raises(ValueError, match="links outside the archive"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_hardlink_with_an_absolute_target_raises(tmp_path: Path) -> None:
    """Hardlinks are in ``filter="data"``'s link check too (it tests ``islnk() or issym()``).

    Probe-verified: ``LNKTYPE {TOP}/b.py -> /etc/passwd`` raises
    ``AbsoluteLinkError`` today, so the fail-loud rule applies to hardlinks
    identically -- a blanket "hardlink -> skip" would turn a branch failure into
    a silent skip.
    """
    tar_path = _write(tmp_path, [_dir(TOP), _lnk(f"{TOP}/b.py", "/etc/passwd")])

    with pytest.raises(ValueError, match="absolute target"):
        list(iter_tar_source_files(tar_path))


@pytest.mark.unit
def test_hardlink_escaping_the_archive_raises(tmp_path: Path) -> None:
    """Probe-verified: ``LNKTYPE -> ../../../etc/passwd`` raises today too."""
    tar_path = _write(tmp_path, [_dir(TOP), _lnk(f"{TOP}/b.py", "../../../etc/passwd")])

    with pytest.raises(ValueError, match="links outside the archive"):
        list(iter_tar_source_files(tar_path))


# --- D7: documented divergences ---------------------------------------------


@pytest.mark.unit
def test_benign_internal_symlink_is_skipped(tmp_path: Path) -> None:
    """The no-divergence case: extracted then skipped by ``is_symlink()`` before, skipped now."""
    tar_path = _write(
        tmp_path,
        [
            _dir(TOP),
            _reg(f"{TOP}/real.py", b"x = 1\n"),
            _sym(f"{TOP}/link.py", "real.py"),
        ],
    )

    assert [pf.path for pf in iter_tar_source_files(tar_path)] == ["real.py"]


@pytest.mark.unit
def test_benign_hardlink_member_is_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DOCUMENTED DIVERGENCE, probe-verified: today the hardlink WAS indexed.

    ``extractall`` materialised it as a real regular file, so the walk saw two
    files (``['a.py', 'b.py']``). A stream cannot materialise it, so it is skipped
    -- loudly, because it is a real (if unreachable) corpus difference. ``git
    archive`` never emits hardlinks.
    """
    tar_path = _write(
        tmp_path,
        [_dir(TOP), _reg(f"{TOP}/a.py", b"x = 1\n"), _lnk(f"{TOP}/b.py", f"{TOP}/a.py")],
    )

    with caplog.at_level(logging.WARNING, logger="indexer.ingest"):
        assert [pf.path for pf in iter_tar_source_files(tar_path)] == ["a.py"]

    assert any("hard link" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_special_file_member_is_skipped_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DOCUMENTED DIVERGENCE: ``extractall`` raised ``SpecialFileError`` and failed the branch.

    Nothing is written to disk now, so the reason to fail loudly is gone -- but
    the member is still worth a WARNING, since a repo containing one is unusual.
    """
    tar_path = _write(tmp_path, [_dir(TOP), _reg(f"{TOP}/a.py", b"x = 1\n"), _fifo(f"{TOP}/pipe")])

    with caplog.at_level(logging.WARNING, logger="indexer.ingest"):
        assert [pf.path for pf in iter_tar_source_files(tar_path)] == ["a.py"]

    assert any("special file" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_duplicate_member_name_keeps_the_first_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """DOCUMENTED DIVERGENCE: ``extractall`` was last-wins; a forward pass is first-wins.

    A single stream cannot look ahead to discover that a later member shadows an
    earlier one, and buffering to find out would defeat the bounded-memory
    property. ``git archive`` emits no duplicate names.
    """
    tar_path = _write(
        tmp_path,
        [_dir(TOP), _reg(f"{TOP}/a.py", b"first = 1\n"), _reg(f"{TOP}/a.py", b"second = 2\n")],
    )

    with caplog.at_level(logging.WARNING, logger="indexer.ingest"):
        files = list(iter_tar_source_files(tar_path))

    assert [pf.content for pf in files] == ["first = 1\n"]
    assert any("duplicate member" in r.getMessage() for r in caplog.records)


# --- resource discipline ----------------------------------------------------


@pytest.mark.unit
def test_tarfile_handle_is_closed_when_the_consumer_abandons_the_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6: the ``try/finally`` around the loop, not a ``with`` at the call site.

    The non-semantic path hands this generator straight into ``index_repo``'s open
    transaction; an exception there abandons it mid-stream, and the handle must
    still be released.
    """
    # Built BEFORE the spy is installed -- `_write` opens a TarFile of its own.
    tar_path = _write(tmp_path, _CLEAN)
    opened = _capture_tarfiles(monkeypatch)

    gen = iter_tar_source_files(tar_path)
    assert next(gen).path == "README.md"
    assert opened[0].closed is False

    gen.close()
    assert opened[0].closed is True
