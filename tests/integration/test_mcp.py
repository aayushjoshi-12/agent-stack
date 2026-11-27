"""MCP client (P43): a real stdio MCP server's tools become tools of the agent."""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from rich.console import Console

from agent_stack.app.application import Application
from agent_stack.app.bootstrap import build_application
from agent_stack.app.config import AppConfig
from agent_stack.approval.auto import rule_verdict
from agent_stack.gateways.base import MessageEnvelope
from agent_stack.gateways.cli import run_cli
from agent_stack.tools.mcp import McpServerConfig, _parse, load_mcp_config
from tests.fakes import FakeChatModel, ai, call

pytestmark = pytest.mark.integration

SERVER = """\
import os

from mcp.server.mcpserver import Context, MCPServer

server = MCPServer("test")


@server.tool()
def add(a: int, b: int) -> str:
    \"\"\"Add two numbers.\"\"\"
    return f"sum={a + b}"


@server.tool()
def pid() -> str:
    \"\"\"The server's process id.\"\"\"
    return str(os.getpid())


@server.tool()
async def grow(ctx: Context) -> str:
    \"\"\"Add a mul tool and tell the client.\"\"\"

    def mul(a: int, b: int) -> str:
        \"\"\"Multiply two numbers.\"\"\"
        return f"product={a * b}"

    server.add_tool(mul)
    await ctx.session.send_tool_list_changed()
    return "grown"


server.run()
"""


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")  # models are built, never called


def _write_config(path: Path, servers: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), "utf-8")


def _app(tmp_path: Path, servers: dict[str, Any], **env: str) -> Application:
    workspace = tmp_path / "ws"
    _write_config(workspace / ".mcp.json", servers)
    values = {
        "OPENROUTER_API_KEY": "sk-or-test",
        "AGENT_STACK_SMALL_MODEL": "none",
        "AGENT_STACK_WORKSPACE": str(workspace),
        "AGENT_STACK_DB": str(tmp_path / "agent.db"),
        "AGENT_STACK_HOME": str(tmp_path / "home"),
        **env,
    }
    return build_application(AppConfig.from_env(values))


def _server(tmp_path: Path, **extra: Any) -> dict[str, Any]:
    script = tmp_path / "server.py"
    script.write_text(SERVER, "utf-8")
    return {"command": sys.executable, "args": [str(script)], **extra}


async def test_a_server_tool_is_called_with_arguments_and_its_result_reaches_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_stack.core import factory as factory_module

    model = FakeChatModel(
        [ai(tool_calls=[call("mcp__test__add", {"a": 2, "b": 40})]), ai("the sum is 42")]
    )
    monkeypatch.setattr(factory_module, "build_model", lambda name, **kw: model)
    app = _app(
        tmp_path,
        {"test": _server(tmp_path, trusted=True), "broken": {"command": "no-such-command-xyz"}},
    )
    assert "mcp__test__add" not in {t.name for t in app.agent.tools}  # nothing starts at wiring
    try:
        handle = await app.send_message(MessageEnvelope(principal="u", session="s", text="add"))
        reply = await handle.result()
        assert reply.content == "the sum is 42"
        assert "mcp__test__add" in {t.name for t in model.bound_tools}
        results = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
        assert [m.content for m in results] == ["sum=42"]

        status = {s.name: s for s in await app.mcp_servers()}
        assert status["test"].state == "ready" and status["test"].tools == 3
        assert status["broken"].state == "failed" and status["broken"].error
        assert "broken" in status["broken"].line() and "failed" in status["broken"].line()
    finally:
        await app.drain_background()
    assert {s.name: s.state for s in app.mcp.status()} == {  # type: ignore[union-attr]
        "test": "stopped",
        "broken": "failed",
    }


async def test_untrusted_server_tools_need_approval_and_go_to_the_classifier_in_auto_mode(
    tmp_path: Path,
) -> None:
    app = _app(
        tmp_path,
        {"test": _server(tmp_path), "safe": _server(tmp_path, trusted=True)},
        AGENT_STACK_EXTERNAL_ACCESS="allow",
    )
    try:
        await app.start_mcp()
        policy = app.agent.tool_policy
        assert policy.needs_approval("mcp__test__add", {"a": 1, "b": 2})
        assert not policy.needs_approval("mcp__safe__add", {"a": 1, "b": 2})
        assert not policy.needs_approval("read_file", {"path": "x"})
        assert rule_verdict("mcp__test__add", {"a": 1}, tmp_path) is None  # classifier decides
    finally:
        await app.drain_background()


def test_an_app_without_mcp_config_has_no_manager(tmp_path: Path) -> None:
    app = build_application(
        AppConfig.from_env(
            {
                "OPENROUTER_API_KEY": "sk-or-test",
                "AGENT_STACK_WORKSPACE": str(tmp_path / "ws"),
                "AGENT_STACK_DB": str(tmp_path / "agent.db"),
                "AGENT_STACK_HOME": str(tmp_path / "home"),
            }
        )
    )
    assert app.mcp is None


def test_workspace_config_wins_per_server_and_vars_are_expanded(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write_config(
        home / "mcp.json",
        {"shared": {"command": "home-cmd"}, "only_home": {"url": "https://x/${HOST}/mcp"}},
    )
    _write_config(
        workspace / ".mcp.json",
        {
            "shared": {
                "command": "ws-cmd",
                "args": ["--key", "${KEY}"],
                "env": {"TOKEN": "${KEY}", "MISSING": "${NOPE}"},
            },
            "bad": {"args": []},
        },
    )
    servers = {
        s.name: s
        for s in load_mcp_config(workspace, home, {"KEY": "secret", "HOST": "example.org"})
    }
    assert servers["shared"].command == "ws-cmd" and servers["shared"].args == ("--key", "secret")
    assert servers["shared"].env == {"TOKEN": "secret", "MISSING": ""}
    assert servers["only_home"].url == "https://x/example.org/mcp"
    assert servers["only_home"].transport == "http"
    assert servers["bad"].error is not None


# P52: lifecycle.


def _tool(app: Application, name: str) -> BaseTool:
    return next(t for t in app.agent.tools if t.name == name)


async def _gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.1)
    return False


async def test_shutdown_through_a_gateway_ends_the_server_process(tmp_path: Path) -> None:
    app = _app(tmp_path, {"test": _server(tmp_path, trusted=True)})
    await app.start_mcp()
    pid = int(await _tool(app, "mcp__test__pid").ainvoke({}))
    await run_cli(app, instream=io.StringIO("exit\n"), out=Console(file=io.StringIO()))
    assert await _gone(pid)
    assert app.mcp is not None and app.mcp.status()[0].state == "stopped"


async def test_a_killed_server_is_restarted_on_the_next_call(tmp_path: Path) -> None:
    app = _app(tmp_path, {"test": _server(tmp_path, trusted=True)})
    try:
        await app.start_mcp()
        pid = int(await _tool(app, "mcp__test__pid").ainvoke({}))
        os.kill(pid, signal.SIGKILL)
        assert await _gone(pid)
        with pytest.raises(RuntimeError, match="restarted on the next call"):
            await _tool(app, "mcp__test__add").ainvoke({"a": 1, "b": 2})
        assert await _tool(app, "mcp__test__add").ainvoke({"a": 1, "b": 2}) == "sum=3"
        new_pid = int(await _tool(app, "mcp__test__pid").ainvoke({}))
        assert new_pid != pid
        assert (await app.mcp_servers())[0].state == "ready"
    finally:
        await app.drain_background()
    assert await _gone(new_pid)


async def test_restarts_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_stack.tools import mcp as mcp_module

    monkeypatch.setattr(mcp_module, "MAX_RESTARTS", 0)
    app = _app(tmp_path, {"test": _server(tmp_path, trusted=True)})
    try:
        await app.start_mcp()
        os.kill(int(await _tool(app, "mcp__test__pid").ainvoke({})), signal.SIGKILL)
        with pytest.raises(RuntimeError, match="restarted on the next call"):
            await _tool(app, "mcp__test__pid").ainvoke({})
        with pytest.raises(RuntimeError, match="not restarted again"):
            await _tool(app, "mcp__test__pid").ainvoke({})
    finally:
        await app.drain_background()


async def test_tools_list_changed_adds_the_new_tool_to_the_agent(tmp_path: Path) -> None:
    app = _app(tmp_path, {"test": _server(tmp_path, trusted=True)})
    try:
        await app.start_mcp()
        assert "mcp__test__mul" not in {t.name for t in app.agent.tools}
        assert await _tool(app, "mcp__test__grow").ainvoke({}) == "grown"
        for _ in range(50):
            if "mcp__test__mul" in {t.name for t in app.agent.tools}:
                break
            await asyncio.sleep(0.1)
        assert await _tool(app, "mcp__test__mul").ainvoke({"a": 6, "b": 7}) == "product=42"
        names = [t.name for t in app.agent.tools if t.name.startswith("mcp__")]
        assert len(names) == len(set(names)) == 4  # swapped, not appended
        assert app.mcp is not None and app.mcp.status()[0].tools == 4
    finally:
        await app.drain_background()


async def test_start_timeout_is_configurable_per_server(tmp_path: Path) -> None:
    slow: McpServerConfig = _parse("slow", {"command": "x", "startTimeout": 5}, {})
    bad = _parse("bad", {"command": "x", "startTimeout": "soon"}, {})
    assert slow.start_timeout == 5.0 and slow.error is None
    assert bad.error is not None and "startTimeout" in bad.error

    app = _app(tmp_path, {"test": _server(tmp_path, startTimeout=0.01)})
    try:
        status = {s.name: s for s in await app.mcp_servers()}
        assert status["test"].state == "failed"
        assert "did not start within 0.01s" in (status["test"].error or "")
    finally:
        await app.drain_background()
