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
