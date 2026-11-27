"""P41: ``Application.export_session`` over the real file transcript, and the web download."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_stack.app.application import Application
from agent_stack.app.config import AppConfig
from agent_stack.core.context import compaction_message
from tests.fakes import FakeChatModel, ai
from tests.integration.test_web_gateway import HEADERS, OWNER, _application, _client, _start

pytestmark = pytest.mark.integration

SECRET = "sk-or-v1-" + "a" * 40


async def _conversation(application: Application) -> str:
    session = await application.new_session(OWNER)
    await application.rename_session(session, "Read the config")
    await application.session_store.append_messages(
        session,
        [
            HumanMessage(content="read config.py"),
            AIMessage(
                content="Reading it.",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "config.py"}, "id": "t1"},
                    {"name": "set_env", "args": {"api_key": "hunter2-long"}, "id": "t2"},
                ],
            ),
            ToolMessage(content="KEY = '" + SECRET + "'\n" + "x" * 5000, tool_call_id="t1"),
            ToolMessage(content="ok", tool_call_id="t2"),
            compaction_message("GOAL: read config.py", files="## config.py\nsecret copy"),
            HumanMessage(content="now summarise it"),
            AIMessage(content="It sets ```KEY```."),
        ],
    )
    return session


async def test_json_export_round_trips_and_is_redacted(tmp_path: Path) -> None:
    application = _application(tmp_path, FakeChatModel([]))
    session = await _conversation(application)

    data = json.loads(await application.export_session(session, "json"))

    assert data["session"]["id"] == session and data["session"]["title"] == "Read the config"
    assert [m["kind"] for m in data["messages"]] == [
        "human", "ai", "tool", "tool", "system", "human", "ai"
    ]  # fmt: skip
    first = data["messages"][0]
    assert first["content"] == "read config.py" and first["ts"] and first["run_id"] is not None
    calls = data["messages"][1]["tool_calls"]
    assert [c["name"] for c in calls] == ["read_file", "set_env"]
    assert calls[1]["args"]["api_key"] == "[redacted]"  # a secret-named argument
    assert SECRET not in json.dumps(data)  # a secret-shaped value in a tool result
    assert data["messages"][2]["tool_call_id"] == "t1"


async def test_markdown_export_has_every_message_tool_call_and_the_compaction(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path, FakeChatModel([]))
    session = await _conversation(application)

    text = await application.export_session(session, "md")

    assert text.startswith("# Read the config\n")
    for wanted in ("read config.py", "now summarise it", "read_file", "set_env", "Reading it."):
        assert wanted in text
    assert '"path": "config.py"' in text  # the arguments
    assert "<details><summary>Result (" in text and "more characters]" in text  # folded, cut
    assert "**Compacted:**" in text and "> GOAL: read config.py" in text
    assert "secret copy" not in text  # the compaction's file copies are not chat
    assert SECRET not in text and "hunter2-long" not in text


async def test_a_bad_format_is_refused(tmp_path: Path) -> None:
    application = _application(tmp_path, FakeChatModel([]))
    session = await _conversation(application)
    with pytest.raises(ValueError, match="md, json"):
        await application.export_session(session, "pdf")


async def test_save_export_writes_into_the_workspace_and_never_overwrites(tmp_path: Path) -> None:
    application = _application(tmp_path, FakeChatModel([]))
    application.config = AppConfig(workspace=tmp_path / "ws")
    session = await _conversation(application)

    first = await application.save_export(session, "md")
    second = await application.save_export(session, "md")
    named = await application.save_export(session, "json", "out/chat.json")

    assert first.parent == tmp_path / "ws" and first.name.startswith("read-the-config-")
    assert second.name == first.stem + "-2.md"
    assert named == tmp_path / "ws" / "out" / "chat.json"
    assert json.loads(named.read_text())["session"]["id"] == session


async def test_web_export_downloads_the_file(tmp_path: Path) -> None:
    application = _application(tmp_path, FakeChatModel([ai("hello there")]))
    async for client in _client(application):
        session, run_id = await _start(client, "say hello")
        await client.get(f"/runs/{run_id}/events", headers=HEADERS)

        md = await client.get(f"/sessions/{session}/export", headers=HEADERS)
        assert md.status_code == 200
        assert md.headers["content-type"].startswith("text/markdown")
        assert md.headers["content-disposition"].startswith("attachment; filename=")
        assert md.headers["content-disposition"].endswith('.md"')
        assert "say hello" in md.text and "hello there" in md.text

        js = await client.get(f"/sessions/{session}/export?fmt=json", headers=HEADERS)
        assert js.headers["content-disposition"].endswith('.json"')
        assert [m["kind"] for m in js.json()["messages"]] == ["human", "ai"]

        assert (
            await client.get(f"/sessions/{session}/export?fmt=pdf", headers=HEADERS)
        ).status_code == 422
        assert (await client.get(f"/sessions/{session}/export")).status_code == 401
        assert (await client.get("/sessions/nope/export", headers=HEADERS)).status_code == 404
