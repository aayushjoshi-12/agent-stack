"""Integration tests for the sandboxed terminal runner (Phase P14)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_stack.core.errors import CommandTimeoutError
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.tools.builtins.terminal import (
    MAX_STREAM_BYTES,
    TRUNCATION_MARKER,
    run_command,
    run_command_tool,
)

pytestmark = pytest.mark.integration


async def test_command_cwd_preserved(tmp_workspace: Path) -> None:
    policy = WorkspacePolicy(tmp_workspace)

    result = await run_command(["pwd"], cwd=policy.root)

    assert result.exit_code == 0
    assert result.stdout.strip() == str(policy.root)


async def test_command_env_is_filtered(
    tmp_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_STACK_SECRET", "leaked")

    result = await run_command(["env"], cwd=tmp_workspace)

    assert result.exit_code == 0
    assert "AGENT_STACK_SECRET" not in result.stdout
    assert "leaked" not in result.stdout
    assert "PATH=" in result.stdout


async def test_command_timeout(tmp_workspace: Path) -> None:
    start = time.perf_counter()

    result = await run_command(["sleep", "5"], cwd=tmp_workspace, timeout=0.3)

    elapsed = time.perf_counter() - start
    assert result.timed_out is True
    assert elapsed < 2.0


async def test_command_timeout_error_path(tmp_workspace: Path) -> None:
    with pytest.raises(CommandTimeoutError):
        await run_command(["sleep", "5"], cwd=tmp_workspace, timeout=0.3, raise_on_timeout=True)


async def test_command_exit_code(tmp_workspace: Path) -> None:
    result = await run_command(["false"], cwd=tmp_workspace)

    assert result.exit_code != 0
    assert result.timed_out is False


async def test_command_output_capped(tmp_workspace: Path) -> None:
    (tmp_workspace / "big.txt").write_text("a" * (MAX_STREAM_BYTES * 4))

    result = await run_command(["cat", "big.txt"], cwd=tmp_workspace)

    assert result.exit_code == 0
    assert result.stdout.endswith(TRUNCATION_MARKER)
    assert len(result.stdout) <= MAX_STREAM_BYTES + len(TRUNCATION_MARKER)


async def test_run_command_tool_defaults_cwd_to_workspace(tmp_workspace: Path) -> None:
    policy = WorkspacePolicy(tmp_workspace)
    tool = run_command_tool(policy)

    rendered = await tool.ainvoke({"command": "pwd"})

    assert str(policy.root) in rendered
