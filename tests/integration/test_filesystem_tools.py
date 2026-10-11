"""Integration tests for the workspace-scoped filesystem tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from agent_stack.core.errors import ToolError, WorkspaceEscapeError
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.tools.builtins.filesystem import MAX_FILE_BYTES, filesystem_tools

pytestmark = pytest.mark.integration


def _tools(root: Path) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in filesystem_tools(WorkspacePolicy(root))}


async def test_write_then_read_round_trip(tmp_workspace: Path) -> None:
    tools = _tools(tmp_workspace)

    await tools["write_file"].ainvoke({"path": "notes/a.txt", "content": "hello\nworld"})

    assert (tmp_workspace / "notes" / "a.txt").read_text() == "hello\nworld"
    assert await tools["read_file"].ainvoke({"path": "notes/a.txt"}) == "hello\nworld"


async def test_list_directory_shows_file(tmp_workspace: Path) -> None:
    tools = _tools(tmp_workspace)
    await tools["write_file"].ainvoke({"path": "a.txt", "content": "x"})

    listing = await tools["list_directory"].ainvoke({"path": "."})

    assert "a.txt" in listing


async def test_edit_file_replaces_text(tmp_workspace: Path) -> None:
    tools = _tools(tmp_workspace)
    await tools["write_file"].ainvoke({"path": "a.txt", "content": "hello world"})

    message = await tools["edit_file"].ainvoke({"path": "a.txt", "old": "world", "new": "there"})

    assert (tmp_workspace / "a.txt").read_text() == "hello there"
    assert "Edited" in message


async def test_edit_file_rejects_missing_text(tmp_workspace: Path) -> None:
    tools = _tools(tmp_workspace)
    await tools["write_file"].ainvoke({"path": "a.txt", "content": "hello"})

    with pytest.raises(ToolError):
        await tools["edit_file"].ainvoke({"path": "a.txt", "old": "nope", "new": "x"})


async def test_search_files_finds_matches(tmp_workspace: Path) -> None:
    tools = _tools(tmp_workspace)
    await tools["write_file"].ainvoke({"path": "pkg/mod.py", "content": "pass"})
    await tools["write_file"].ainvoke({"path": "pkg/notes.md", "content": "note"})

    found = await tools["search_files"].ainvoke({"pattern": "*.py", "path": "."})

    assert found == "pkg/mod.py"


async def test_read_file_outside_workspace_rejected(tmp_path: Path, tmp_workspace: Path) -> None:
    tools = _tools(tmp_workspace)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")

    with pytest.raises(WorkspaceEscapeError):
        await tools["read_file"].ainvoke({"path": "../outside.txt"})

    with pytest.raises(WorkspaceEscapeError):
        await tools["read_file"].ainvoke({"path": str(outside)})


async def test_oversize_write_rejected(tmp_workspace: Path) -> None:
    tools = _tools(tmp_workspace)

    with pytest.raises(ToolError):
        await tools["write_file"].ainvoke(
            {"path": "big.txt", "content": "a" * (MAX_FILE_BYTES + 1)}
        )

    assert not (tmp_workspace / "big.txt").exists()
