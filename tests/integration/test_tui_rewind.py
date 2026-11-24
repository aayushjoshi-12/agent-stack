"""The TUI's esc-esc rewind, against the real application, store and files."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_stack.gateways.base import MessageEnvelope
from agent_stack.gateways.tui.app import TuiApp
from agent_stack.gateways.tui.widgets import PromptArea, RewindScreen
from tests.fakes import FakeChatModel, ai, call
from tests.integration.test_changes import World, _world

pytestmark = pytest.mark.integration


async def _two_turns(tmp_path: Path) -> World:
    model = FakeChatModel(
        [
            ai(tool_calls=[call("write_file", {"path": "a.txt", "content": "v1\n"})]),
            ai("wrote a"),
            ai(tool_calls=[call("write_file", {"path": "a.txt", "content": "v2\n"})]),
            ai("changed a"),
        ]
    )
    w = _world(tmp_path, model)
    for text in ("make a", "change a"):
        handle = await w.app.send_message(
            MessageEnvelope(principal="tui:p", session="s1", text=text)
        )
        await handle.result()
    return w


async def _settle(pilot: object) -> None:
    for _ in range(8):
        await pilot.pause()  # type: ignore[attr-defined]


async def test_double_escape_opens_the_picker_and_rewinds_code_and_conversation(
    tmp_path: Path,
) -> None:
    w = await _two_turns(tmp_path)
    app = TuiApp(w.app, session_id="s1")
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(pilot)
        await pilot.press("escape")
        assert not isinstance(app.screen, RewindScreen)  # one escape only focuses the prompt
        await pilot.press("escape")
        await _settle(pilot)
        assert isinstance(app.screen, RewindScreen)

        await pilot.press("enter")  # newest message is highlighted: "change a"
        await _settle(pilot)
        await pilot.press("enter")  # first choice: code and conversation
        await _settle(pilot)

        assert (w.workspace / "a.txt").read_text() == "v1\n"  # code put back
        assert app._session_id not in (None, "s1")  # now in the fork
        prompt = app.query_one("#prompt", PromptArea)
        assert prompt.text == "change a"  # the rewound message, ready to edit


async def test_double_escape_clears_typed_text_instead_of_rewinding(tmp_path: Path) -> None:
    w = await _two_turns(tmp_path)
    app = TuiApp(w.app, session_id="s1")
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(pilot)
        app.query_one("#prompt", PromptArea).load_text("half a thought")
        await pilot.press("escape", "escape")
        await _settle(pilot)
        assert not isinstance(app.screen, RewindScreen)
        assert app.query_one("#prompt", PromptArea).text == ""


async def test_escape_in_the_picker_goes_back_then_closes(tmp_path: Path) -> None:
    w = await _two_turns(tmp_path)
    app = TuiApp(w.app, session_id="s1")
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(pilot)
        await pilot.press("escape", "escape")
        await _settle(pilot)
        await pilot.press("enter")  # to the choices
        await _settle(pilot)
        await pilot.press("escape")  # back to the list
        await _settle(pilot)
        assert isinstance(app.screen, RewindScreen)
        await pilot.press("escape")  # close
        await _settle(pilot)
        assert not isinstance(app.screen, RewindScreen)
        assert (w.workspace / "a.txt").read_text() == "v2\n"  # nothing changed


async def test_opening_the_original_warns_and_the_palette_action_restores_its_files(
    tmp_path: Path,
) -> None:
    w = await _two_turns(tmp_path)
    app = TuiApp(w.app, session_id="s1")
    async with app.run_test(size=(120, 36)) as pilot:
        await _settle(pilot)
        await pilot.press("escape", "escape")
        await _settle(pilot)
        await pilot.press("enter", "enter")  # rewind code + conversation
        await _settle(pilot)
        fork = app._session_id
        assert (w.workspace / "a.txt").read_text() == "v1\n" and fork != "s1"

        await app._open_session("s1")  # as the chats picker does
        await _settle(pilot)
        assert (w.workspace / "a.txt").read_text() == "v1\n"  # opening alone leaves files be
        assert any("files in the folder" in n.message for n in app._notifications)

        await app.action_restore_files()
        await _settle(pilot)
        assert (w.workspace / "a.txt").read_text() == "v2\n"  # now the original's state
