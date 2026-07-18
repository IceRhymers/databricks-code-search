"""Unit tests for indexer.fetch using httpx.MockTransport + in-memory tarballs."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx
import pytest

import indexer.fetch as fetch
from indexer.fetch import download_tarball, extract_tarball, resolve_ref

ORG = "acme"
REPO = "widgets"
BRANCH = "main"
SHA = "abc1234def5678"
TOP_DIR = f"{ORG}-{REPO}-{SHA[:7]}"
CODELOAD_URL = "https://codeload.github.com/acme/widgets/tar.gz/abc1234"


def _make_tarball(names_to_content: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in names_to_content.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


CLEAN_TARBALL = _make_tarball(
    {
        f"{TOP_DIR}/README.md": b"# hello\n",
        f"{TOP_DIR}/src/main.py": b"def f():\n    return 1\n",
    }
)


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == f"/repos/{ORG}/{REPO}":
        return httpx.Response(200, json={"default_branch": BRANCH})
    if path == f"/repos/{ORG}/{REPO}/commits/{BRANCH}":
        return httpx.Response(200, json={"sha": SHA})
    if path == f"/repos/{ORG}/{REPO}/tarball/{SHA}":
        return httpx.Response(302, headers={"location": CODELOAD_URL})
    if request.url == httpx.URL(CODELOAD_URL):
        return httpx.Response(200, content=CLEAN_TARBALL)
    return httpx.Response(404)


def _client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(_handler))


@pytest.mark.unit
def test_resolve_ref_returns_branch_and_sha() -> None:
    with _client() as client:
        assert resolve_ref(client, ORG, REPO) == (BRANCH, SHA)


@pytest.mark.unit
def test_download_tarball_follows_redirect_and_writes_bytes(tmp_path: Path) -> None:
    with _client() as client:
        out = download_tarball(client, ORG, REPO, SHA, tmp_path)
    assert out == tmp_path / "source.tar.gz"
    assert out.read_bytes() == CLEAN_TARBALL


@pytest.mark.unit
def test_download_tarball_rejects_oversized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cap below the served tarball size -> the streamed download aborts.
    monkeypatch.setattr(fetch, "MAX_TARBALL_BYTES", 4)
    with _client() as client:
        with pytest.raises(ValueError, match="exceeds"):
            download_tarball(client, ORG, REPO, SHA, tmp_path)


@pytest.mark.unit
def test_extract_tarball_rejects_decompression_bomb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cap below the summed member size -> extraction is refused before writing.
    monkeypatch.setattr(fetch, "MAX_EXTRACTED_BYTES", 4)
    tar_path = tmp_path / "source.tar.gz"
    tar_path.write_bytes(CLEAN_TARBALL)
    dest = tmp_path / "extracted"
    with pytest.raises(ValueError, match="exceeding"):
        extract_tarball(tar_path, dest)
    assert not any(dest.iterdir()) if dest.exists() else True


@pytest.mark.unit
def test_extract_tarball_yields_top_level_dir(tmp_path: Path) -> None:
    tar_path = tmp_path / "source.tar.gz"
    tar_path.write_bytes(CLEAN_TARBALL)
    root = extract_tarball(tar_path, tmp_path / "extracted")
    assert root.name == TOP_DIR
    assert (root / "README.md").read_text() == "# hello\n"
    assert (root / "src" / "main.py").exists()


@pytest.mark.unit
def test_extract_tarball_neutralizes_path_traversal(tmp_path: Path) -> None:
    malicious = _make_tarball(
        {
            f"{TOP_DIR}/ok.py": b"x = 1\n",
            "../evil.txt": b"pwned\n",
        }
    )
    tar_path = tmp_path / "evil.tar.gz"
    tar_path.write_bytes(malicious)
    dest = tmp_path / "extracted"

    # The data filter blocks the escaping member; nothing is written outside dest.
    with pytest.raises(tarfile.OutsideDestinationError):
        extract_tarball(tar_path, dest)
    assert not (dest.parent / "evil.txt").exists()
