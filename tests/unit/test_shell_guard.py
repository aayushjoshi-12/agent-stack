from pathlib import Path

from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.tools.builtins.terminal import outside_paths, run_command_tool


def test_flags_paths_outside_the_workspace(tmp_path: Path) -> None:
    assert outside_paths("find / -name rag-pipeline", tmp_path)
    assert outside_paths("cat ~/.ssh/id_rsa", tmp_path)
    assert outside_paths("sed -i s/a/b/ /Users/someone/repos/x/app.py", tmp_path)


def test_allows_workspace_and_system_paths(tmp_path: Path) -> None:
    inside = f"cat {tmp_path}/a.txt && ls ./b && /usr/bin/env python3 x.py > /dev/null"
    assert outside_paths(inside, tmp_path) == []
    assert outside_paths("echo hi > /tmp/out.txt; https://x.org/a/b", tmp_path) == []


async def test_tool_refuses_and_can_be_unconfined(tmp_path: Path) -> None:
    policy = WorkspacePolicy(root=tmp_path)
    refused = await run_command_tool(policy).ainvoke(
        {"command": "ls /Users", "shell": True}
    )
    assert refused.startswith("Error: this command touches paths outside the workspace")
    allowed = await run_command_tool(policy, confine=False).ainvoke(
        {"command": "echo ok", "shell": True}
    )
    assert "ok" in allowed
