from __future__ import annotations

from pathlib import Path

from agent_stack.gateways.base import MessageEnvelope
from agent_stack.skills import SkillLoader, SkillRegistry
from tests.fakes import FakeChatModel, ai
from tests.unit.test_application import _build_application


def _skills(tmp_path: Path) -> SkillRegistry:
    folder = tmp_path / "skills" / "release-notes"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: Draft release notes from git history.\n---\n\n"
        "# Release notes\nRun git log and group the changes.\n"
    )
    return SkillRegistry(SkillLoader([tmp_path / "skills"]))


async def test_skills_are_listed_as_slash_commands(tmp_path: Path) -> None:
    app, _, _ = _build_application(FakeChatModel([ai("ok")]))
    assert app.slash_commands() == []
    app.skills = _skills(tmp_path)
    [command] = app.slash_commands()
    assert (command.name, command.kind) == ("release-notes", "skill")
    assert command.description.startswith("Draft release notes")


async def test_typing_a_skill_sends_its_instructions_but_the_chat_shows_what_you_typed(
    tmp_path: Path,
) -> None:
    model = FakeChatModel([ai("done")])
    app, _, _ = _build_application(model)
    app.skills = _skills(tmp_path)

    handle = await app.send_message(
        MessageEnvelope(principal="u", session="s1", text="/release-notes for v0.2")
    )
    await handle.result()
    sent = str(model.calls[0][-1].content)
    assert sent.startswith("/release-notes for v0.2")
    assert "Run git log and group the changes." in sent and "for v0.2" in sent

    shown = [h.text for h in await app.session_history("s1") if h.kind == "user"]
    assert shown == ["/release-notes for v0.2"]  # the body is not chat


async def test_underscores_match_hyphens_and_non_skills_pass_through(tmp_path: Path) -> None:
    app, _, _ = _build_application(FakeChatModel([ai("a"), ai("b"), ai("c")]))
    app.skills = _skills(tmp_path)
    assert app.expand_slash("/release_notes") is not None  # Telegram spells it this way
    assert app.expand_slash("/RELEASE-NOTES x") is not None
    assert app.expand_slash("/unknown-thing x") is None
    assert app.expand_slash("/etc/hosts is odd") is None  # a path, not a command
    assert app.expand_slash("plain text") is None
