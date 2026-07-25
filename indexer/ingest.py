"""Stream a downloaded repo tarball once and yield the text files worth indexing.

This is the streaming counterpart of :func:`indexer.parse.iter_source_files`, and
the only file source the indexing job uses (issue #106). It exists for two
reasons:

* **One decompression, not two.** ``extract_tarball`` used to call
  ``tf.getmembers()`` (a full gzip decompression, to build the member list) and
  then ``tf.extractall()`` (a second full decompression, plus a write of every
  byte to disk). This module walks the stream exactly once with ``tf.next()``:
  there is no ``getmembers()`` and no ``extractall()`` anywhere in ``indexer/``.
* **No extracted tree.** Nothing is written to disk, so one worker's peak local
  disk is the compressed tarball alone (``MAX_TARBALL_BYTES``), not the tarball
  plus its expansion. See ``indexer.fetch.REQUIRED_FREE_BYTES``.

**Ordering contract: ARCHIVE order, and the contract is determinism plus set
equality -- never sequence equality.** There is no cheap metadata pass over a
gzip stream (``tarfile`` locates each header by walking the decompressed bytes,
so a names-only pass costs the very decompression this module removes), and
sorting would mean materialising every ``ParsedFile`` in memory, destroying the
bounded-memory property the non-semantic path depends on. A GitHub tarball is
``git archive`` order -- git tree order for a fixed commit -- so the sequence is
deterministic per ``head_sha``. Nothing downstream depends on it:
``indexer.store``'s per-file classification and its sweep are both order-free.

**``indexer.parse.iter_source_files`` is this module's executable oracle.** It has
no production caller any more, and is deliberately kept: ``tests/unit/
test_ingest_parity.py`` extracts each fixture tarball to disk, runs
``iter_source_files`` over the tree and this module over the same tarball, and
requires identical ``(path, lang, size, content)`` sets. The expected value is
computed live from ``parse.py`` rather than from a golden fixture, so it cannot
be regenerated to match a drifting implementation. ``parse.py`` is also watched
by ``tests/unit/test_semantics_version_tripwire.py``; retiring it is a deliberate
semantics-version conversation and is out of scope here.

The filter chain below MUST stay identical to ``parse.py``'s: ``.git/`` skip,
``MAX_FILE_BYTES`` skip, NUL-in-the-first-8-KB binary sniff, UTF-8 decode or
skip, strip surviving NULs. ``_looks_binary`` and ``_BINARY_SNIFF_BYTES`` are
imported from ``parse`` rather than re-implemented -- private on purpose, since a
forked copy of the sniff rule is exactly how the two would silently diverge (the
8 KB window itself rides along inside it; ``_BINARY_SNIFF_BYTES`` is deliberately
NOT imported separately, as an unused import).
Two details are load-bearing for that parity and are *not* self-evident:
``size`` is the archive-declared ``member.size`` (the analogue of
``stat().st_size``), not ``len(content)`` -- it exceeds it after NUL stripping --
and ``lang`` is derived from the *relative* path's lowered suffix, after the top
directory is stripped.

Behavioural divergence from the old extract-then-walk path, PROBE-VERIFIED
against ``extract_tarball`` + ``iter_source_files`` on Python 3.12 (several of
these contradict what the ``tarfile`` docs suggest; do not re-derive them from
memory):

===================================== ================================== ==========================
Member shape                          Old (extract-to-disk, verified)    New (stream)
===================================== ================================== ==========================
benign internal symlink               extracted, then skipped            skipped -- identical
symlink with an absolute target       ``AbsoluteLinkError``, branch      ``ValueError`` -- same
                                      fails                              fail-loud posture
symlink escaping the archive          ``LinkOutsideDestinationError``    ``ValueError`` -- same
hard link, benign internal target     materialised as a file and         skipped + WARNING
                                      INDEXED                            (git has no hardlinks)
hard link, absolute/escaping target   same link errors as symlinks       ``ValueError`` -- same
device / FIFO / socket                ``SpecialFileError``, branch       skipped + WARNING
                                      fails                              (nothing is written now)
``/etc/passwd``                       leading ``/`` stripped, extracted  ``ValueError``
                                      outside the top dir, SILENTLY
                                      IGNORED
``/{TOP}/evil.py``                    leading ``/`` stripped -- INDEXED  ``ValueError``
``../evil.txt``                       ``OutsideDestinationError``        ``ValueError``
``{TOP}/../evil.txt``                 normalised to ``evil.txt``,        ``ValueError``
                                      SILENTLY IGNORED
``{TOP}/../{TOP}/evil.py``            normalised back inside --          ``ValueError``
                                      INDEXED
``./{TOP}/a.py``                      top dir NOT stripped, yielded as   ``a.py``
                                      ``{TOP}/a.py``
empty member name                     ``IsADirectoryError``              ``ValueError``
regular file named exactly ``{TOP}``  yielded nothing                    ``ValueError``
duplicate member name                 last wins                         first wins + WARNING
two or more top-level dirs            ``ValueError``                     ``ValueError``
zero members / no top-level dir       ``ValueError``                     ``ValueError``
===================================== ================================== ==========================

The four silent rows (two absolute names, two ``..`` names) are latent
path-confusion bugs this module closes: two members were silently dropped and
two were silently INDEXED under a path the archive did not really contain.
Absolute and escaping link targets deliberately keep today's hard failure rather
than relaxing to a skip -- nothing is written to disk any more so a skip would be
safe, but changing which repos index is out of scope here.

Validation (the bomb cap, the top-level-dir check, path safety, any ``tarfile``
corruption error, and the trailing-stream drain) now happens as the stream is
consumed. On the non-semantic path that is inside ``index_repo``'s open
transaction, so a malformed archive surfaces as a rolled-back transaction rather
than a pre-connection failure. The branch-level outcome is unchanged
(``status="failed"``).
"""

from __future__ import annotations

import logging
import posixpath
import tarfile
from collections.abc import Iterator
from pathlib import Path

from indexer.languages import EXT_TO_LANG, MAX_FILE_BYTES, ParsedFile

# Private on purpose: the binary-sniff rule must be un-forkable, so it is
# imported from the oracle module rather than re-implemented here. Precedent:
# tests/unit/test_parse.py already imports _BINARY_SNIFF_BYTES.
from indexer.parse import _looks_binary

logger = logging.getLogger("indexer.ingest")

# Upper bound on the uncompressed content one branch may stream out of its
# tarball. Moved here from indexer.fetch by #106, and re-scoped with it: nothing
# is written to disk any more, so this is a WORK cap (a decompression-bomb
# guard), not a disk cap. Enforced incrementally, two ways:
#
#   * every regular member's declared size, accumulated BEFORE the
#     .git/size/binary filters -- exactly what the old up-front
#     `sum(m.size for m in members if m.isreg())` counted, and still the tighter
#     check for the case that matters (one huge tracked blob);
#   * `member.offset`, the tar stream's cumulative decompressed position, which
#     bounds members of ANY type. Regular-file accounting alone would let an
#     archive of a million directory or link headers decompress unbounded --
#     they carry no data, so `streamed` never moves.
MAX_EXTRACTED_BYTES = 2_000_000_000


def _normalise_member_name(name: str) -> str:
    """Return ``name`` with a leading ``./`` stripped, rejecting unsafe shapes.

    The ONLY normalisation performed is stripping a leading ``./`` (GNU tar
    writes those; the old path left them on and consequently failed to strip the
    top-level directory at all). Everything else raises.

    ``..`` is rejected as a **raw path component**, before any normalisation, and
    ``posixpath.normpath`` is deliberately NOT used here -- normalising first
    hides two of the three traversal shapes the D7 table requires to raise::

        raw name                    normpath()          normpath-then-check
        ../evil.txt                 ../evil.txt         raise
        {TOP}/../evil.txt           evil.txt            ALLOW  <- wrong
        {TOP}/../{TOP}/evil.py      {TOP}/evil.py       ALLOW  <- wrong, and it
                                                        then passes the top-dir
                                                        check and is indexed

    For a member *name* the question is "does it contain ``..`` at all", not
    "where does it resolve to"; ``normpath`` belongs only in the link-target
    check, where the second question is the real one. The empty name needs its
    own test because ``normpath("")`` returns ``"."``.
    """
    if name.startswith("./"):
        name = name[2:]
    if not name:
        raise ValueError("tarball contains a member with an empty name")
    if posixpath.isabs(name):
        raise ValueError(f"tarball member has an absolute name: {name!r}")
    if ".." in name.split("/"):
        raise ValueError(f"tarball member name contains a '..' component: {name!r}")
    return name


def _assert_link_target_is_contained(name: str, linkname: str) -> None:
    """Raise ``ValueError`` if ``linkname`` is absolute or escapes the archive.

    Pure ``posixpath`` string arithmetic, NOT a transliteration of CPython's
    ``filter="data"`` check: that one computes
    ``os.path.realpath(os.path.join(dest, os.path.dirname(name), linkname))``,
    and there is no destination directory in a stream -- substituting a
    placeholder would resolve against the real filesystem and the process cwd,
    which is a silently wrong check rather than a strict one.

    Diverges from ``filter="data"`` only for links chained through other
    symlinks, which ``git archive`` cannot produce.
    """
    if posixpath.isabs(linkname):
        raise ValueError(f"tarball member {name!r} links to an absolute target: {linkname!r}")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), linkname))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError(f"tarball member {name!r} links outside the archive: {linkname!r}")


def iter_tar_source_files(tar_path: Path) -> Iterator[ParsedFile]:
    """Yield a :class:`ParsedFile` per indexable text file in ``tar_path``.

    A drop-in replacement for ``indexer.parse.iter_source_files(root)``: same
    return type, same element semantics, one argument. ONE forward pass over the
    archive -- no ``getmembers()``, no ``extractall()``, nothing written to disk.

    ``path`` is repo-relative (the single top-level ``org-repo-<sha7>/``
    directory GitHub tarballs carry is stripped). Skips directories, links,
    special files, ``.git/`` contents, members declared larger than
    ``MAX_FILE_BYTES``, duplicate paths, binaries (NUL sniff) and UTF-8 decode
    failures. Raises ``ValueError`` for an unsafe member name or link target, for
    an archive with anything other than exactly one top-level directory, and when
    the streamed content exceeds :data:`MAX_EXTRACTED_BYTES`.

    Peak memory is ``MAX_FILE_BYTES`` plus the seen-path set (measured with
    ``tracemalloc`` at ~120 bytes per path -- the set retains the path STRING,
    not just a slot -- so ~12 MB at 100k files), held for as long as the consumer
    holds the generator, which on the non-semantic path is the whole open
    transaction.

    ``mode="r:gz"``, not ``"r:*"``: GitHub only ever serves gzip, and pinning the
    codec removes bzip2/LZMA from the attack surface entirely. Their compression
    ratios are far higher than DEFLATE's, so an ``r:*`` reader would accept a
    much smaller upload for the same decompressed volume.
    """
    tf = tarfile.open(tar_path, mode="r:gz")
    try:
        top_dir: str | None = None
        streamed = 0
        seen: set[str] = set()

        while (member := tf.next()) is not None:
            # FIRST statement of the body, not the last: TarFile.next() appends
            # every TarInfo to tf.members, and unlike extract_tarball (whose
            # member list died before engine.connect()) this TarFile stays open
            # for the whole transaction. At the END of the body every `continue`
            # below would skip it -- which is most members in a real repo -- and
            # the list would grow unbounded. Safe because next() only appends
            # and extractfile() reads member.offset_data, never the list; the
            # `while ... tf.next()` loop is what makes it safe (TarFile.__iter__
            # indexes into self.members and would be corrupted by this).
            #
            # The ignore is typeshed's gap, not a runtime one: `TarFile.members`
            # is a plain list assigned in `TarFile.__init__` and appended to by
            # `next()`, but the stub only declares `getmembers()` (which would
            # read the WHOLE archive -- exactly what AC1 removes).
            tf.members.clear()  # type: ignore[attr-defined]

            name = _normalise_member_name(member.name)

            component = name.split("/", 1)[0]
            if top_dir is None:
                top_dir = component
            elif component != top_dir:
                raise ValueError(
                    "expected exactly one top-level dir in tarball, found "
                    f"{sorted({top_dir, component})}"
                )

            # Before the isreg() gate, because links are isreg()-false. The old
            # path ran filter="data"'s link check on `islnk() or issym()` alike,
            # so hardlinks are in scope for the same fail-loud rule.
            if member.islnk() or member.issym():
                _assert_link_target_is_contained(name, member.linkname)

            # BEFORE the isreg() gate, so it bounds members of EVERY type --
            # including the ones that carry no data and so never move `streamed`
            # below. `member.offset` is this header's position in the DECOMPRESSED
            # tar stream and is monotonic across a forward pass, so it is the
            # honest measure of how much has actually been decompressed. Without
            # it an archive of a million directory or link headers decompresses
            # unbounded past a cap that only ever sums regular-file sizes.
            if member.offset > MAX_EXTRACTED_BYTES:
                raise ValueError(
                    f"tarball stream reaches {member.offset} decompressed bytes at member "
                    f"{name!r}, exceeding {MAX_EXTRACTED_BYTES}"
                )

            if not member.isreg():
                if member.islnk():
                    # Divergence: extractall() materialised these as real files
                    # and they WERE indexed. git archives contain no hardlinks.
                    logger.warning("skipping hard link member %s in %s", name, tar_path.name)
                elif not (member.isdir() or member.issym()):
                    # Divergence: extractall() raised SpecialFileError and failed
                    # the branch. Nothing is written to disk now, so the reason
                    # to fail loudly is gone.
                    logger.warning("skipping special file member %s in %s", name, tar_path.name)
                continue

            # Counted BEFORE the filters below, exactly like the old up-front
            # `sum(m.size for m in members if m.isreg())`: same threshold, same
            # population, only the timing differs. Still the tighter check for a
            # single oversized member, whose declared size is caught from its
            # header before any of its data is read.
            streamed += member.size
            if streamed > MAX_EXTRACTED_BYTES:
                raise ValueError(
                    f"tarball streams to {streamed} bytes of content, "
                    f"exceeding {MAX_EXTRACTED_BYTES}"
                )

            rel_path = name[len(top_dir) + 1 :] if name != top_dir else ""
            if not rel_path:
                raise ValueError(f"tarball member is the top-level dir itself: {name!r}")

            if ".git" in rel_path.split("/"):
                continue

            # From the header alone -- the analogue of parse.py's stat() before
            # read_bytes(), so an oversized blob's data is never read at all.
            if member.size > MAX_FILE_BYTES:
                continue

            if rel_path in seen:
                # extractall() was last-wins; a single forward pass cannot look
                # ahead, so this is first-wins. git archives have no duplicates.
                logger.warning("skipping duplicate member %s in %s", name, tar_path.name)
                continue
            seen.add(rel_path)

            fh = tf.extractfile(member)
            if fh is None:
                # Unreachable: the isreg() gate above already excludes every
                # member for which extractfile() returns None. This narrows
                # tarfile's declared `IO[bytes] | None` for mypy; it is not a
                # real branch.
                continue
            with fh:
                raw = fh.read()

            if _looks_binary(raw):
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue

            # Postgres `text` rejects NUL (0x00), which is legal UTF-8 and so can
            # survive the 8 KB sniff window (issue #37). Stripped before the
            # yield so stored content and its content_sha stay consistent --
            # `size` stays the archive-declared length and may exceed
            # len(content), matching parse.py's on-disk size.
            content = content.replace("\x00", "")

            yield ParsedFile(
                path=rel_path,
                lang=EXT_TO_LANG.get(Path(rel_path).suffix.lower()),
                size=member.size,
                content=content,
            )

        # A truncated archive is NOT reliably a hard error on the read path:
        # `TarFile.next()` only re-raises a truncated/invalid header when
        # `self.offset == 0`, so a cut mid-stream can simply return None and end
        # the loop early. That yields a PARTIAL file set, which is worse than a
        # zero-file one -- the missing files are absent from `index_repo`'s seen
        # set, so the membership sweep deletes them from the corpus. Draining the
        # remaining bytes forces gzip's CRC32/ISIZE trailer check, which a
        # truncated or corrupt stream cannot pass. On a well-formed archive this
        # reads only the trailing block padding.
        fileobj = tf.fileobj
        if fileobj is not None:
            while fileobj.read(65536):
                pass

        if top_dir is None:
            # A zero-member (empty) archive. This raise is the one guard against a
            # bug that is invisible in production: without it the branch indexes
            # zero files, index_repo skips its sweep on the empty-seen-set guard,
            # and _stamp_repo_branch(seen_any=False) writes (head_sha, unchanged
            # version) -- which for a branch already stamped at the current
            # INDEX_SEMANTICS_VERSION matches the skip seam exactly, silently
            # marking it current at a HEAD it never read.
            raise ValueError("expected exactly one top-level dir in tarball, found none")
    finally:
        # A consumer that abandons this generator (gen.close(), or an exception
        # out of index_repo's transaction) still releases the file handle.
        tf.close()
