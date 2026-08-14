"""Static checks for the skill-only Claude Code plugin (issue #123).

The plugin intentionally distributes guidance only. It never registers an MCP server, embeds a
Databricks App URL, or asks a consumer for configuration; server setup remains the explicit,
client-owned procedure in README.md. These checks are hermetic so they run under ``make test``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT / "claude-plugin"
_PLUGIN_DOTDIR = _PLUGIN_ROOT / ".claude-plugin"
_PLUGIN_MANIFEST = _PLUGIN_DOTDIR / "plugin.json"
_SKILL_PATH = _PLUGIN_ROOT / "skills" / "cross-repo-search" / "SKILL.md"
_MARKETPLACE_MANIFEST = _REPO_ROOT / ".claude-plugin" / "marketplace.json"
_README = _REPO_ROOT / "README.md"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _split_skill() -> tuple[dict[str, Any], str]:
    text = _SKILL_PATH.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with a YAML frontmatter fence"
    _, frontmatter, body = text.split("---\n", 2)
    return yaml.safe_load(frontmatter), body


@pytest.mark.unit
def test_plugin_contains_a_manifest_and_skill_but_no_mcp_registration() -> None:
    """The plugin is intentionally skill-only: no hidden or automatic MCP discovery."""
    assert _PLUGIN_MANIFEST.is_file()
    assert _SKILL_PATH.is_file()
    assert not (_PLUGIN_ROOT / ".mcp.json").exists()
    assert not (_PLUGIN_DOTDIR / ".mcp.json").exists()

    manifest = _load_json(_PLUGIN_MANIFEST)
    assert "userConfig" not in manifest
    assert "register" not in manifest["description"].lower()


@pytest.mark.unit
def test_marketplace_entry_targets_the_skill_only_plugin() -> None:
    manifest = _load_json(_PLUGIN_MANIFEST)
    marketplace = _load_json(_MARKETPLACE_MANIFEST)
    assert marketplace["name"]
    assert marketplace["description"]
    assert marketplace["owner"]
    assert len(marketplace["plugins"]) == 1

    entry = marketplace["plugins"][0]
    assert entry["name"] == manifest["name"]
    assert (_REPO_ROOT / entry["source"] / ".claude-plugin" / "plugin.json").is_file()
    assert "register" not in entry["description"].lower()


_DESCRIPTION_TRIGGERS = (
    r"not in the current workspace",
    r"where else",
    r"which repositor",
    r"across repositor",
)
_TOOL_NAMES = (
    "list_repos",
    "search_code",
    "semantic_search",
    "find_references",
    "list_imports",
)
_CANDIDATE_SET_RE = re.compile(r"(?=[\s\S]*candidate)(?=[\s\S]*(?:grep|LSP))", re.I)


@pytest.mark.unit
def test_skill_has_request_text_triggers_and_tool_guidance() -> None:
    frontmatter, body = _split_skill()
    description = frontmatter["description"]
    for pattern in _DESCRIPTION_TRIGGERS:
        assert re.search(pattern, description, re.I), f"description missing trigger: {pattern!r}"
    for name in _TOOL_NAMES:
        assert name in body, f"skill body never names {name}"
    assert _CANDIDATE_SET_RE.search(body)
    assert "untrusted data" in body.lower()


@pytest.mark.unit
def test_skill_requires_explicit_manual_setup_when_tools_are_absent() -> None:
    """A missing tool list must not make the agent invent results or registration state."""
    _, body = _split_skill()
    assert "does **not** register" in body
    assert "Do not fabricate results" in body
    assert "workspace-only" in body
    assert "README" in body


@pytest.mark.unit
def test_readme_keeps_manual_registration_and_skill_install_separate() -> None:
    readme = _README.read_text(encoding="utf-8")
    assert "claude plugin marketplace add" in readme
    assert "claude mcp add code-search" in readme
    assert "register an MCP server" in readme
    assert "--config APP_URL" not in readme
