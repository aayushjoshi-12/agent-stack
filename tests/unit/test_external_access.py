"""The outside-the-workspace setting, through the real application wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_stack.app.bootstrap import build_application
from agent_stack.app.config import AppConfig
from agent_stack.core.errors import ConfigurationError


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")  # models are built, never called


def _app(tmp_path: Path, **env: str):  # type: ignore[no-untyped-def]
    values = {
        "OPENROUTER_API_KEY": "sk-or-test",
        "AGENT_STACK_WORKSPACE": str(tmp_path),
        "AGENT_STACK_DB": str(tmp_path / "agent.db"),
        "AGENT_STACK_HOME": str(tmp_path / "home"),
        **env,
    }
    return build_application(AppConfig.from_env(values))


def test_ask_is_the_default_and_gates_outside_calls_even_for_read_tools(tmp_path: Path) -> None:
    app = _app(tmp_path)
    policy = app.agent.tool_policy
    assert policy.needs_approval("read_file", {"path": "/etc/hosts"})
    assert policy.needs_approval("list_directory", {"path": "../sibling"})
    assert not policy.needs_approval("read_file", {"path": "notes.md"})
    assert policy.needs_approval("run_command", {"command": "ls"})  # gated by name as before
    assert app.get_approval_mode() == "auto"


async def test_ask_lets_the_file_tools_reach_outside_once_approved(tmp_path: Path) -> None:
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "a.txt").write_text("secret-ish")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = _app(workspace, AGENT_STACK_DB=str(workspace / "agent.db"))
    tools = {t.name: t for t in app.agent.tools}
    assert "secret-ish" in await tools["read_file"].ainvoke({"path": str(outside / "a.txt")})


async def test_deny_refuses_both_tools_and_shell(tmp_path: Path) -> None:
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "a.txt").write_text("x")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    app = _app(
        workspace, AGENT_STACK_DB=str(workspace / "agent.db"), AGENT_STACK_EXTERNAL_ACCESS="deny"
    )
    tools = {t.name: t for t in app.agent.tools}
    assert not app.agent.tool_policy.needs_approval("read_file", {"path": "/etc/hosts"})
    with pytest.raises(Exception, match="outside"):
        await tools["read_file"].ainvoke({"path": str(outside / "a.txt")})
    # (temp folders count as scratch space, so name a path in a home folder instead)
    refused = await tools["run_command"].ainvoke(
        {"command": "cat /Users/nobody/a.txt", "shell": True}
    )
    assert refused.startswith("Error: this command touches paths outside the workspace")


def test_the_settings_are_validated_and_the_old_switch_still_works() -> None:
    assert AppConfig.from_env({}).external_access == "ask"
    assert AppConfig.from_env({"AGENT_STACK_SHELL_CONFINED": "0"}).external_access == "allow"
    assert AppConfig.from_env({"AGENT_STACK_EXTERNAL_ACCESS": "DENY"}).external_access == "deny"
    assert AppConfig.from_env({"AGENT_STACK_APPROVAL_MODE": "auto"}).approval_mode == "auto"
    assert AppConfig.from_env({"AGENT_STACK_AUTO_MODEL": "off"}).auto_model is None
    assert AppConfig.from_env({}).auto_model == "openrouter:typesafe/jev-router"
    for bad in ({"AGENT_STACK_EXTERNAL_ACCESS": "maybe"}, {"AGENT_STACK_APPROVAL_MODE": "yolo"}):
        with pytest.raises(ConfigurationError):
            AppConfig.from_env(bad)
