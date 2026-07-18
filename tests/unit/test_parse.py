"""Unit tests for indexer.parse.iter_source_files against a temp tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from indexer.languages import MAX_FILE_BYTES
from indexer.parse import iter_source_files


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "acme-widgets-abc1234"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()

    (root / "src" / "main.py").write_text("def f():\n    return 1\n")
    (root / "src" / "app.js").write_text("function f(){}\n")
    (root / "main.go").write_text("package main\n")
    (root / "Widget.java").write_text("class Widget {}\n")
    (root / "lib.rs").write_text("fn f() {}\n")
    (root / "README.md").write_text("# hi\n")  # unknown ext -> lang None, still stored
    (root / "data.bin").write_bytes(b"\x00\x01\x02binary\x00")  # binary -> skipped
    (root / "big.py").write_text("x = 1\n" * (MAX_FILE_BYTES // 2))  # oversized -> skipped
    (root / ".git" / "config").write_text("[core]\n")  # under .git -> skipped
    return root


@pytest.mark.unit
def test_language_detection(tree: Path) -> None:
    by_path = {pf.path: pf for pf in iter_source_files(tree)}
    assert by_path["src/main.py"].lang == "python"
    assert by_path["src/app.js"].lang == "javascript"
    assert by_path["main.go"].lang == "go"
    assert by_path["Widget.java"].lang == "java"
    assert by_path["lib.rs"].lang == "rust"


@pytest.mark.unit
def test_unknown_extension_text_stored_with_lang_none(tree: Path) -> None:
    by_path = {pf.path: pf for pf in iter_source_files(tree)}
    assert "README.md" in by_path
    assert by_path["README.md"].lang is None
    assert by_path["README.md"].content == "# hi\n"


@pytest.mark.unit
def test_binary_oversized_and_git_are_skipped(tree: Path) -> None:
    paths = {pf.path for pf in iter_source_files(tree)}
    assert "data.bin" not in paths
    assert "big.py" not in paths
    assert not any(p.startswith(".git/") for p in paths)


@pytest.mark.unit
def test_size_is_byte_length(tree: Path) -> None:
    by_path = {pf.path: pf for pf in iter_source_files(tree)}
    assert by_path["src/main.py"].size == len(b"def f():\n    return 1\n")
