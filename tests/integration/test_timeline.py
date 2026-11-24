"""Rewind, fork, switch files, the branch tree and references, on real SQLite and real files."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_stack.app.timeline import parse_ref, render_ascii, render_mermaid
from agent_stack.core.errors import PersistenceError
from agent_stack.gateways.base import MessageEnvelope
from tests.fakes import FakeChatModel, ai, call
from tests.integration.test_changes import World, _world

pytestmark = pytest.mark.integration


async def _say(w: World, session: str, text: str) -> None:
    handle = await w.app.send_message(MessageEnvelope(principal="u1", session=session, text=text))
    await handle.result()


async def _two_turns(tmp_path: Path) -> World:
    model = FakeChatModel(
        [
            ai(tool_calls=[call("write_file", {"path": "a.txt", "content": "v1\n"})]),
            ai("wrote a"),
            ai(tool_calls=[call("write_file", {"path": "a.txt", "content": "v2\n"})]),
            ai(tool_calls=[call("write_file", {"path": "b.txt", "content": "new\n"})]),
            ai("edited a and wrote b"),
        ]
    )
    w = _world(tmp_path, model)
    await _say(w, "s1", "make a")
    await _say(w, "s1", "change a, add b")
    return w


async def test_turns_list_the_points_and_the_files_each_touched(tmp_path: Path) -> None:
    w = await _two_turns(tmp_path)
    turns = await w.app.timeline.turns("s1")
    assert [t.user_text for t in turns] == ["make a", "change a, add b"]
    assert [t.files_changed for t in turns] == [1, 2]
    assert all(t.completed_event_id for t in turns)


async def test_rewind_undoes_later_files_and_keeps_the_original_to_come_back_to(
    tmp_path: Path,
) -> None:
    w = await _two_turns(tmp_path)
    assert (w.workspace / "a.txt").read_text() == "v2\n" and (w.workspace / "b.txt").exists()

    branch, result = await w.app.timeline.rewind("s1", "1")  # after turn 1

    assert result.ok
    assert (w.workspace / "a.txt").read_text() == "v1\n"  # put back as it was after turn 1
    assert not (w.workspace / "b.txt").exists()  # created later: removed
    branch_session = await w.app.get_session(branch)
    assert branch_session is not None and branch_session.branch_of == "s1"
    original = await w.app.get_session("s1")
    assert original is not None
    assert len(await w.app.session_store.get_messages("s1")) > len(
        await w.app.session_store.get_messages(branch)
    )  # nothing was removed from the original

    back = await w.app.timeline.switch_files(branch, "s1")  # go back to the original's files
    assert back.ok
    assert (w.workspace / "a.txt").read_text() == "v2\n" and (w.workspace / "b.txt").exists()


async def test_a_hand_edited_file_is_a_conflict_unless_forced(tmp_path: Path) -> None:
    w = await _two_turns(tmp_path)
    (w.workspace / "a.txt").write_text("my own edit\n")

    _, result = await w.app.timeline.rewind("s1", "1")

    assert result.conflicts == ["a.txt"]
    assert (w.workspace / "a.txt").read_text() == "my own edit\n"  # left alone
    assert not (w.workspace / "b.txt").exists()  # the untouched file still went back
    branch, forced = await w.app.timeline.rewind("s1", "1", force=True)
    assert forced.ok and (w.workspace / "a.txt").read_text() == "v1\n"
    assert branch


async def test_rewind_without_files_leaves_the_folder_alone(tmp_path: Path) -> None:
    w = await _two_turns(tmp_path)
    _, result = await w.app.timeline.rewind("s1", "1", restore_files=False)
    assert result.moved == [] and (w.workspace / "b.txt").exists()


async def test_a_fork_keeps_its_file_history_after_the_parent_is_deleted(tmp_path: Path) -> None:
    w = await _two_turns(tmp_path)
    branch = await w.app.timeline.fork("s1", "1")
    await w.app.delete_session("s1")
    entries = await w.store.entries(branch)
    assert [(e.path, e.after) for e in entries] == [("a.txt", "v1\n")]


async def test_the_tree_shows_every_branch_with_its_files(tmp_path: Path) -> None:
    w = await _two_turns(tmp_path)
    one = await w.app.timeline.fork("s1", "1")
    two = await w.app.timeline.fork("s1", "1-")  # before turn 1: nothing yet
    tree = await w.app.timeline.tree(one)  # asked from a branch, rooted at the original
    assert tree.session.id == "s1"
    assert {c.session.id for c in tree.children} == {one, two}
    by_id = {c.session.id: c for c in tree.children}
    assert [f[0] for f in by_id[one].files] == ["a.txt"] and by_id[two].files == []
    text = render_ascii(tree, current=one)
    assert f"* {one}" in text and "o s1" in text and "a.txt" in text
    mermaid = render_mermaid(tree, current=one)
    assert mermaid.startswith("flowchart TD") and "-->" in mermaid


async def test_a_reference_describes_a_point_and_opens_as_a_new_conversation(
    tmp_path: Path,
) -> None:
    w = await _two_turns(tmp_path)
    ref = await w.app.timeline.reference("s1", "1")
    assert parse_ref(ref)[0] == "s1"

    text = await w.app.timeline.describe(ref)
    assert "USER: make a" in text and "a.txt" in text and "+v1" in text
    assert "change a, add b" not in text and "b.txt" not in text  # only up to that point

    opened = await w.app.timeline.open_reference(ref)
    assert opened != "s1" and [e.path for e in await w.store.entries(opened)] == ["a.txt"]

    with pytest.raises(PersistenceError):
        parse_ref("https://example.org")
    with pytest.raises(PersistenceError):
        await w.app.timeline.event("s1", "9")


async def test_the_command_line_drives_the_same_operations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from agent_stack.observability import history_cli

    w = await _two_turns(tmp_path)
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    history_cli.add_parsers(sub)

    async def cli(*argv: str) -> tuple[int, str]:
        code = await history_cli.run(w.app, parser.parse_args(list(argv)))
        return code, capsys.readouterr().out

    code, out = await cli("turns", "s1")
    assert code == 0 and "make a" in out and "2 file(s)" in out
    code, out = await cli("ref", "make", "s1", "1")
    assert code == 0 and out.startswith("asref://")
    ref = out.strip()
    code, out = await cli("ref", "show", ref)
    assert code == 0 and "USER: make a" in out
    code, out = await cli("rewind", "s1", "1")
    assert code == 0 and "deleted b.txt" in out and "wrote a.txt" in out
    branch = out.split()[2]
    code, out = await cli("tree", branch, "--html", str(tmp_path / "t.html"))
    assert code == 0 and "o s1" in out and (tmp_path / "t.html").read_text().count("mermaid") >= 2
    code, out = await cli("switch", branch, "s1")
    assert code == 0 and (w.workspace / "b.txt").exists()
    code, out = await cli("rewind", "s1", "99")
    assert code == 1 and out.startswith("error:")
    code, out = await cli("sessions")
    assert code == 0 and "(branch of s1)" in out


async def test_agent_tools_name_read_and_draw(tmp_path: Path) -> None:
    from agent_stack.app.timeline_tools import timeline_tools
    from agent_stack.core.agent import Agent
    from agent_stack.core.run_context import RunContext, run_context

    w = await _two_turns(tmp_path)
    tools = {t.name: t for t in timeline_tools(lambda: w.app)}
    context = RunContext(
        run_id="r", session_id="s1", user_id="u", depth=0, parent_run_id=None,
        agent=Agent(name="a", model="test:m"), services=None,  # type: ignore[arg-type]
    )  # fmt: skip
    assert "inside a run" in await tools["show_branches"].ainvoke({})
    with run_context(context):
        ref = await tools["reference_here"].ainvoke({})
        assert ref.startswith("asref://")
        assert "a.txt" in await tools["read_reference"].ainvoke({"reference": ref})
        assert "* s1" in await tools["show_branches"].ainvoke({})  # the current one is starred
    assert (await tools["read_reference"].ainvoke({"reference": "nope"})).startswith("Error")


async def test_lineage_runs_through_forks_and_rewind_to_works_from_a_branch(
    tmp_path: Path,
) -> None:
    w = await _two_turns(tmp_path)
    first = (await w.app.timeline.lineage("s1"))[0]
    assert first.turn.user_text == "make a" and first.session_id == "s1"

    # rewind to before turn 2 (conversation + code), then look at the lineage of the fork
    out = await w.app.timeline.rewind_to("s1", (await w.app.timeline.lineage("s1"))[1])
    assert out.prompt == "change a, add b" and out.files.ok
    assert (w.workspace / "a.txt").read_text() == "v1\n" and not (w.workspace / "b.txt").exists()
    line = await w.app.timeline.lineage(out.session_id)
    assert [(lt.turn.user_text, lt.session_id) for lt in line] == [("make a", "s1")]

    # from the fork, go back further than the fork point: the owner is the original
    again = await w.app.timeline.rewind_to(out.session_id, line[0])
    assert again.session_id not in ("s1", out.session_id)
    assert not (w.workspace / "a.txt").exists()  # before turn 1 nothing existed


async def test_code_only_rewind_keeps_the_conversation_and_records_the_restore(
    tmp_path: Path,
) -> None:
    w = await _two_turns(tmp_path)
    target = (await w.app.timeline.lineage("s1"))[1]  # before turn 2
    out = await w.app.timeline.rewind_to("s1", target, conversation=False)
    assert out.session_id == "s1" and out.files.ok
    assert (w.workspace / "a.txt").read_text() == "v1\n"
    entries = await w.store.entries("s1")
    assert entries[-1].run_id == "rewind" and entries[-1].after == "v1\n"
    # the conversation still has both turns
    assert len(await w.app.timeline.turns("s1")) == 2


async def test_conversation_only_rewind_leaves_the_files(tmp_path: Path) -> None:
    w = await _two_turns(tmp_path)
    target = (await w.app.timeline.lineage("s1"))[1]
    out = await w.app.timeline.rewind_to("s1", target, code=False)
    assert out.session_id != "s1" and out.files.moved == []
    assert (w.workspace / "a.txt").read_text() == "v2\n"
