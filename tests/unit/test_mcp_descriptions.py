"""Unit tests for the MCP discoverability surface added in app/main.py (issue #121):
``build_mcp()``'s server-level ``instructions=`` and the per-tool ``title=``/``annotations=``.

No DB, no SDK session: ``FastMCP.instructions`` is a plain property and ``list_tools()`` is
in-process metadata introspection over the registered tool functions, so these run under
``tests/unit/`` (unlike ``tests/integration/test_mcp_server.py``, which is ``@pytest.mark.e2e``
and never executes under ``make test``'s ``-m "unit or observability"`` selection).
"""

from __future__ import annotations

import re

import pytest

from app.main import SERVER_INSTRUCTIONS, build_mcp
from app.query.parser import _SUPPORTED

# --------------------------------------------------------------------------------- instructions


@pytest.mark.unit
def test_instructions_present_and_routes_discovery() -> None:
    instr = build_mcp().instructions
    assert instr is not None
    assert instr.strip() != ""
    assert "list_repos" in instr
    # Without these, the AC is unenforced: the pre-change baseline is ``instructions=None``, so
    # *any* non-empty string would pass. These pin the not-found-locally trigger specifically.
    assert re.search(r"working directory", instr, re.I)
    assert re.search(r"cannot find locally", instr, re.I)
    # Binds this test's subject to the constant the other test checks. Without this, a different
    # string passed to ``FastMCP(instructions=...)`` in ``build_mcp()`` could evade the denylist
    # and length cap in ``test_instructions_boundary_denylist_and_length_cap`` below while still
    # passing this test.
    assert instr == SERVER_INSTRUCTIONS


@pytest.mark.unit
def test_instructions_boundary_denylist_and_length_cap() -> None:
    """Denylist is PRIMARY, the 650-char cap is secondary. An earlier draft smuggled the entire
    query grammar into ``instructions`` at 731 chars under a 900-char cap while hitting zero
    denylist tokens -- a length-only check does not catch grammar creep, so the denylist is the
    real boundary and the cap is only a backstop against unbounded growth.

    The field-name half of the denylist is DERIVED from ``_SUPPORTED`` rather than hand-copied
    (the repo convention stated at app/query/semantic_filters.py:26-27: field names are recovered
    by inverting the parser's own map, "never a second hand-written table") so a future field
    added to the query grammar is denylisted automatically.
    """
    instr = SERVER_INSTRUCTIONS
    grammar = {f"{f}:" for f in _SUPPORTED} | {
        "/regex/",
        "next_cursor",
        "max_bytes",
        "truncation_reason",
    }
    for token in grammar:
        assert token not in instr, f"instructions leaked query-grammar token: {token!r}"
    assert len(instr) <= 650


# --------------------------------------------------------------------------------- tool metadata

_EXPECTED_TOOLS = {
    "search_code",
    "semantic_search",
    "list_repos",
    "get_file",
    "find_references",
    "list_imports",
}


@pytest.mark.unit
async def test_tool_metadata_titles_and_annotations() -> None:
    tools = await build_mcp().list_tools()
    names = {tool.name for tool in tools}
    assert names == _EXPECTED_TOOLS
    for tool in tools:
        assert tool.description
        assert tool.title
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.openWorldHint is True
