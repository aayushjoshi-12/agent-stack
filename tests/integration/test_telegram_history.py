"""/rewind, /branches, /ref and /files in the Telegram gateway, on the real application."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_stack.gateways.telegram import TelegramGateway
from tests.fakes import FakeChatModel, ai, call
from tests.integration.test_changes import World, _world
from tests.unit.test_telegram_gateway import (
    OWNER,
    FakeApi,
    _callback,
    _message,
    _say,
    _settings,
)

pytestmark = pytest.mark.integration


async def _gateway(tmp_path: Path) -> tuple[TelegramGateway, FakeApi, World]:
    model = FakeChatModel(
        [
            ai(tool_calls=[call("write_file", {"path": "a.txt", "content": "v1\n"})]),
            ai("wrote a"),
            ai(tool_calls=[call("write_file", {"path": "a.txt", "content": "v2\n"})]),
            ai("changed a"),
        ]
    )
    w = _world(tmp_path, model)
    api = FakeApi()
    gateway = TelegramGateway(w.app, api, _settings(), edit_interval=0.0)  # type: ignore[arg-type]
    await _say(gateway, "make a")
    await _say(gateway, "change a")
    return gateway, api, w


async def test_rewind_walks_buttons_restores_code_and_returns_the_message(
    tmp_path: Path,
) -> None:
    gateway, api, w = await _gateway(tmp_path)
    original = gateway._chats[OWNER].session
    await gateway.handle_update(_message("/rewind"))
    text, markup = api.sent[-1][1], api.sent[-1][2]
    assert "which message" in text
    buttons = [b["text"] for row in markup["inline_keyboard"] for b in row]
    assert buttons == ["change a", "make a"]  # newest first

    await gateway.handle_update(_callback("rw:1"))  # "change a"
    assert "Restore what?" in api.edits[-1][2]
    await gateway.handle_update(_callback("rm:1:b"))  # code and conversation

    assert (w.workspace / "a.txt").read_text() == "v1\n"
    assert gateway._chats[OWNER].session != original
    assert "1 file(s) restored" in api.edits[-1][2]
    assert api.texts()[-1] == "change a"  # the rewound message, ready to resend


async def test_branches_ref_and_files(tmp_path: Path) -> None:
    gateway, api, w = await _gateway(tmp_path)
    first = gateway._chats[OWNER].session

    await gateway.handle_update(_message("/ref"))
    ref = (
        api.sent[-1][1].split("<code>")[-1].split("</code>")[0]
        if "<code>" in api.sent[-1][1]
        else api.sent[-1][1]
    )
    assert "asref://" in api.sent[-1][1]
    ref = "asref://" + api.sent[-1][1].split("asref://")[1].split("</code>")[0].split()[0]

    await gateway.handle_update(_message(f"/ref {ref}"))  # open it as a new conversation
    assert gateway._chats[OWNER].session != first
    assert "/files" in api.texts()[-1]

    await gateway.handle_update(_message("/files"))  # put that point's files in the folder
    assert (w.workspace / "a.txt").read_text() == "v2\n"  # the reference was to the last turn

    await gateway.handle_update(_message("/branches"))
    assert "<pre>" in api.sent[-1][1] and "a.txt" in api.sent[-1][1]


async def test_bad_input_is_answered_not_raised(tmp_path: Path) -> None:
    gateway, api, _ = await _gateway(tmp_path)
    await gateway.handle_update(_message("/ref https://example.org"))
    assert api.texts()[-1].startswith("Error:")
    await gateway.handle_update(_callback("rw:9"))
    assert api.answers[-1][1].startswith("That list is out of date")
