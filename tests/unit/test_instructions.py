"""Unit tests for :mod:`agent_stack.context.instructions`."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agent_stack.context import InstructionsLoader
from agent_stack.core.errors import InstructionsError

TRUNCATED_MARKER = "…[truncated]"


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


async def test_loads_root_agents_md(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write(root / "AGENTS.md", "# Project\n\nRoot instructions.")
    loader = InstructionsLoader()

    files = await loader.load(root)

    assert len(files) == 1
    assert files[0].path == (root / "AGENTS.md").resolve()
    assert files[0].content == "# Project\n\nRoot instructions."
    assert files[0].size_bytes == len(b"# Project\n\nRoot instructions.")


async def test_nested_agents_md_nearest_wins(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "src" / "sub"
    nested.mkdir(parents=True)
    _write(project / "AGENTS.md", "root content")
    _write(nested / "AGENTS.md", "nested content")
    loader = InstructionsLoader()

    files = await loader.load(nested, stop_at=project)

    # Both files are present, nearest first, and the root file keeps its own content
    # (it does not duplicate or shadow the nested file).
    assert [f.path for f in files] == [
        (nested / "AGENTS.md").resolve(),
        (project / "AGENTS.md").resolve(),
    ]
    assert [f.content for f in files] == ["nested content", "root content"]


async def test_supports_claude_md_fallback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    only_claude = project / "claude-only"
    both = project / "both"
    only_claude.mkdir(parents=True)
    both.mkdir()
    _write(only_claude / "CLAUDE.md", "claude fallback content")
    _write(both / "AGENTS.md", "agents preferred content")
    _write(both / "CLAUDE.md", "claude ignored content")
    loader = InstructionsLoader()

    fallback = await loader.load(only_claude, stop_at=project)
    preferred = await loader.load(both, stop_at=project)

    # A directory with only CLAUDE.md falls back to it.
    assert len(fallback) == 1
    assert fallback[0].path == (only_claude / "CLAUDE.md").resolve()
    assert fallback[0].content == "claude fallback content"

    # AGENTS.md wins when both exist in the same directory; CLAUDE.md is not loaded.
    assert [f.path for f in preferred] == [(both / "AGENTS.md").resolve()]
    assert preferred[0].content == "agents preferred content"


async def test_instruction_size_cap_enforced(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)
    _write(nested / "AGENTS.md", "nested")  # 6 bytes
    _write(project / "AGENTS.md", "x" * 100)  # far over the 20-byte per-file cap
    loader = InstructionsLoader(max_file_bytes=20, max_total_bytes=32)

    files = await loader.load(nested, stop_at=project)

    assert files[0].content == "nested"
    # The oversized root file is truncated to the per-file cap with a marker.
    truncated = files[1]
    assert truncated.path == (project / "AGENTS.md").resolve()
    assert truncated.size_bytes == 100
    assert truncated.content == ("x" * 6) + TRUNCATED_MARKER
    assert len(truncated.content.encode("utf-8")) == 20

    # A tighter total cap drops the furthest file (partial fits allowed).
    tight = InstructionsLoader(max_file_bytes=20, max_total_bytes=6)
    files_tight = await tight.load(nested, stop_at=project)

    assert [f.path for f in files_tight] == [(nested / "AGENTS.md").resolve()]


async def test_cache_invalidates_on_mtime_change(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    _write(path, "first version")
    loader = InstructionsLoader()

    first = await loader.load(tmp_path)
    assert first[0].content == "first version"

    # Unchanged mtime → the same cached InstructionFile object is reused.
    second = await loader.load(tmp_path)
    assert second[0] is first[0]

    # Rewriting and bumping the mtime invalidates the cache entry.
    _write(path, "second version")
    future = time.time() + 5.0
    os.utime(path, (future, future))

    third = await loader.load(tmp_path)
    assert third[0].content == "second version"
    assert third[0] is not first[0]


async def test_unreadable_instructions_file_raises(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").mkdir()  # exists (it is a directory) but cannot be read
    loader = InstructionsLoader()

    with pytest.raises(InstructionsError):
        await loader.load(tmp_path)


async def test_missing_files_are_ignored(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    files = await InstructionsLoader().load(empty)

    assert files == []