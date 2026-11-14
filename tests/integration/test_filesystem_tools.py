"""Integration tests for the workspace-scoped filesystem tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from agent_stack.core.errors import ToolError, WorkspaceEscapeError
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.tools.builtins.filesystem import (
    MAX_FILE_BYTES,
    MAX_SEARCH_RESULTS,
    filesystem_tools,
)

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


# ── grep_files: search file *contents* ───────────────────────────────────────
def _tree(root: Path, files: dict[str, str | bytes]) -> None:
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content)


async def _grep(root: Path, pattern: str, **kwargs: object) -> str:
    return str(await _tools(root)["grep_files"].ainvoke({"pattern": pattern, **kwargs}))


async def test_grep_finds_matching_lines_with_path_and_line_number(tmp_workspace: Path) -> None:
    _tree(
        tmp_workspace,
        {
            "src/a.py": "x = 1\n# TODO: fix this\ny = 2\n",
            "src/b.py": "def f():\n    pass  # TODO later\n",
            "notes.md": "nothing here\n",
        },
    )

    found = await _grep(tmp_workspace, "TODO")

    assert found.splitlines() == [
        "src/a.py:2: # TODO: fix this",
        "src/b.py:2:     pass  # TODO later",
    ]


async def test_grep_is_a_regex_and_can_ignore_case(tmp_workspace: Path) -> None:
    _tree(tmp_workspace, {"a.txt": "Error: boom\nerror again\nfine\n"})

    assert (await _grep(tmp_workspace, r"^error")).splitlines() == ["a.txt:2: error again"]
    both = await _grep(tmp_workspace, r"^error", ignore_case=True)
    assert both.splitlines() == ["a.txt:1: Error: boom", "a.txt:2: error again"]


async def test_grep_can_be_limited_to_a_glob_and_a_subdirectory(tmp_workspace: Path) -> None:
    _tree(
        tmp_workspace,
        {"a.py": "needle\n", "a.md": "needle\n", "sub/b.py": "needle\n", "sub/c.txt": "needle\n"},
    )

    assert (await _grep(tmp_workspace, "needle", glob="*.py")).splitlines() == [
        "a.py:1: needle",
        "sub/b.py:1: needle",
    ]
    assert (await _grep(tmp_workspace, "needle", path="sub")).splitlines() == [
        "sub/b.py:1: needle",
        "sub/c.txt:1: needle",
    ]
    assert (await _grep(tmp_workspace, "needle", path="a.md")).splitlines() == ["a.md:1: needle"]


async def test_grep_skips_junk_directories_binaries_huge_files_and_secrets(
    tmp_workspace: Path,
) -> None:
    _tree(
        tmp_workspace,
        {
            "keep.py": "needle\n",
            ".git/config": "needle\n",
            "node_modules/pkg/index.js": "needle\n",
            ".venv/lib/x.py": "needle\n",
            "pkg/__pycache__/m.py": "needle\n",
            ".agent/logs/audit.jsonl": "needle\n",
            ".env": "API_KEY=needle\n",
            ".env.local": "API_KEY=needle\n",
            ".env.example": "API_KEY=needle\n",
            "blob.bin": b"needle\x00\x01\x02",
            "huge.txt": "needle\n" + "x" * (MAX_FILE_BYTES + 10),
        },
    )

    found = await _grep(tmp_workspace, "needle")

    assert sorted(found.splitlines()) == [".env.example:1: API_KEY=needle", "keep.py:1: needle"]


async def test_grep_stops_at_the_cap_and_says_so(tmp_workspace: Path) -> None:
    _tree(tmp_workspace, {"many.txt": "hit\n" * (MAX_SEARCH_RESULTS + 50)})

    found = await _grep(tmp_workspace, "hit")

    lines = found.splitlines()
    assert len([ln for ln in lines if ln.startswith("many.txt:")]) == MAX_SEARCH_RESULTS
    assert "truncated" in lines[-1]


async def test_grep_truncates_very_long_lines(tmp_workspace: Path) -> None:
    _tree(tmp_workspace, {"min.js": "needle" + "x" * 5000 + "\n"})

    (line,) = (await _grep(tmp_workspace, "needle")).splitlines()

    assert len(line) < 400 and line.endswith("…")


async def test_grep_reports_no_matches_and_bad_input_clearly(tmp_workspace: Path) -> None:
    _tree(tmp_workspace, {"a.txt": "hello\n"})
    tools = _tools(tmp_workspace)

    assert "no matches" in await _grep(tmp_workspace, "zzz")
    with pytest.raises(ToolError, match="regular expression"):
        await tools["grep_files"].ainvoke({"pattern": "(unclosed"})
    with pytest.raises(ToolError, match="empty"):
        await tools["grep_files"].ainvoke({"pattern": ""})
    with pytest.raises(ToolError, match="No such"):
        await tools["grep_files"].ainvoke({"pattern": "x", "path": "missing"})


async def test_grep_cannot_leave_the_workspace_or_follow_links_out(
    tmp_workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle outside\n")
    _tree(tmp_workspace, {"inside.txt": "needle inside\n"})
    (tmp_workspace / "link_dir").symlink_to(outside, target_is_directory=True)
    (tmp_workspace / "link_file.txt").symlink_to(outside / "secret.txt")
    tools = _tools(tmp_workspace)

    found = await _grep(tmp_workspace, "needle")

    assert found.splitlines() == ["inside.txt:1: needle inside"]  # no leak through either link
    with pytest.raises(WorkspaceEscapeError):
        await tools["grep_files"].ainvoke({"pattern": "needle", "path": str(outside)})


def test_the_explorer_gets_grep_and_it_needs_no_approval() -> None:
    from agent_stack.app.bootstrap import _READ_ONLY_TOOLS
    from agent_stack.app.config import AppConfig

    assert "grep_files" in _READ_ONLY_TOOLS
    assert "grep_files" not in AppConfig().approval_tools  # read-only: never gated
