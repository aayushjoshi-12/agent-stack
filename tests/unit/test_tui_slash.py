"""The TUI's / commands: completion list, built-ins, skills, unknown names, approval mode."""

from __future__ import annotations

from typing import Any

from textual.widgets import OptionList

from agent_stack.gateways.base import SlashCommand
from agent_stack.gateways.tui.app import TuiApp
from agent_stack.gateways.tui.widgets import HelpScreen, PromptArea
from tests.unit.gateway_fakes import FakeApplication, event


async def _settle(pilot: Any, n: int = 6) -> None:
    for _ in range(n):
        await pilot.pause()


def _app() -> tuple[TuiApp, FakeApplication]:
    fake = FakeApplication()
    fake.skill_commands = [
        SlashCommand("release-notes", "Draft release notes from git history.", "skill"),
        SlashCommand("review", "Review the current diff.", "skill"),
    ]
    return TuiApp(fake, session_id="s1"), fake  # type: ignore[arg-type]


def _names(app: TuiApp) -> list[str]:
    listing = app.query_one("#completions", OptionList)
    return [str(listing.get_option_at_index(i).id) for i in range(listing.option_count)]


async def test_typing_a_slash_lists_commands_and_skills_and_narrows_as_you_type() -> None:
    app, _ = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        assert app.query_one("#completions", OptionList).display is False
        prompt.load_text("/")
        await _settle(pilot)
        popup = app.query_one("#completions", OptionList)
        assert popup.display is True
        assert {"new", "rewind", "release-notes", "review"} <= set(_names(app))
        prompt.load_text("/re")
        await _settle(pilot)
        assert _names(app)[:3] == ["rewind", "review", "release-notes"][:3] or "review" in _names(
            app
        )
        assert all("re" in n for n in _names(app))
        prompt.load_text("/zzz")
        await _settle(pilot)
        assert popup.display is False
        prompt.load_text("/release-notes now")  # arguments: the list gets out of the way
        await _settle(pilot)
        assert popup.display is False


async def test_tab_and_enter_complete_then_enter_runs() -> None:
    app, fake = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        prompt.load_text("/rel")
        await _settle(pilot)
        await pilot.press("tab")
        await _settle(pilot)
        assert prompt.text == "/release-notes "
        assert app.query_one("#completions", OptionList).display is False

        prompt.load_text("/rev")
        await _settle(pilot)
        await pilot.press("enter")  # completes, does not send
        await _settle(pilot)
        assert prompt.text == "/review " and fake.sent == []


async def test_a_skill_command_is_sent_as_typed_and_a_builtin_is_not_sent() -> None:
    app, fake = _app()
    fake._events = [event(e) for e in ()]  # no events: the run just ends
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        prompt.load_text("/review the auth change")
        await pilot.press("enter")
        await _settle(pilot)
        assert [env.text for env, _, _ in fake.sent] == ["/review the auth change"]

        before = len(fake.sent)
        prompt.load_text("/help")
        await pilot.press("enter")
        await _settle(pilot)
        assert isinstance(app.screen, HelpScreen) and len(fake.sent) == before


async def test_unknown_commands_are_refused_but_paths_are_ordinary_text() -> None:
    app, fake = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        prompt.load_text("/nonsense")
        await pilot.press("enter")
        await _settle(pilot)
        assert fake.sent == []
        assert any("Unknown command" in n.message for n in app._notifications)

        prompt.load_text("/etc/hosts looks odd")
        await pilot.press("enter")
        await _settle(pilot)
        assert [env.text for env, _, _ in fake.sent] == ["/etc/hosts looks odd"]


async def test_shift_tab_and_slash_mode_switch_auto_mode_and_show_it() -> None:
    app, fake = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        assert fake.get_approval_mode() == "ask"
        await pilot.press("shift+tab")
        await _settle(pilot)
        assert fake.get_approval_mode() == "auto"
        hints = app.query_one("#hints")
        assert "auto mode" in str(hints.render()) and hints.display
        prompt = app.query_one("#prompt", PromptArea)
        prompt.load_text("/mode ask")
        await pilot.press("enter")
        await _settle(pilot)
        assert fake.get_approval_mode() == "ask"
        assert not app.query_one("#hints").display  # ask mode: no line at all


async def test_the_prompt_keeps_the_keyboard_after_clicks_and_stray_typing() -> None:
    app, _ = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        app.query_one("#chat").focus()
        await _settle(pilot)
        await pilot.press("h", "i")
        await _settle(pilot)
        assert app.focused is prompt and prompt.text == "hi"

        await pilot.click("#chat")
        await _settle(pilot)
        assert app.focused is prompt

        await pilot.press("f1")  # a dialog keeps its own keys
        await _settle(pilot)
        await pilot.press("x")
        await _settle(pilot)
        assert prompt.text == "hi"


async def test_model_command_shows_and_switches_the_model() -> None:
    app, fake = _app()
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        prompt.load_text("/model openrouter:other/model")
        await pilot.press("enter")
        await _settle(pilot)
        assert fake.get_model() == "openrouter:other/model"
        assert fake.sent == []  # a built-in, not a message
