"""P50: deleting a chat removes what it owns; GC removes what deleted chats left behind.

Real SQLite and real files in tmp dirs; nothing is faked.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_stack.artifacts.local import LocalArtifactStore
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.session import Session
from agent_stack.observability import cli
from agent_stack.persistence.changes import LocalChangeStore
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.event_log import SqliteEventLogStore
from agent_stack.persistence.gc import StorageCollector
from agent_stack.persistence.sqlite import SESSION_TABLES, SqliteDatabase, SqliteSessionStore

LATER = datetime.now(UTC) + timedelta(days=2)  # everything made in the test is past the grace


class World:
    def __init__(self, root: Path) -> None:
        self.folders = root / ".agent" / "sessions" / "sessions"
        self.db = SqliteDatabase(root / "agent.db")
        self.conversation = FileConversationStore(root / ".agent" / "sessions")
        self.log = SqliteEventLogStore(self.db)
        self.sessions = SqliteSessionStore(self.db, self.conversation, self.log)
        self.changes = LocalChangeStore(self.db, root / ".agent" / "changes")
        self.artifacts = LocalArtifactStore(root / ".agent" / "artifacts")
        self.gc = StorageCollector(
            self.db,
            self.sessions,
            self.conversation,
            self.changes,
            sessions_root=root / ".agent" / "sessions" / "sessions",
            artifacts_root=self.artifacts.root,
            changes_root=root / ".agent" / "changes",
        )

    async def chat(self, session_id: str, *, run: str | None = None, artifact: str = "") -> None:
        """A chat with one run, one logged event and a tool result naming ``artifact``."""
        await self.sessions.save(Session(id=session_id, owner_id="u", agent_name="assistant"))
        call = {"name": "run_command", "args": {}, "id": "c1"}
        await self.sessions.append_messages(
            session_id,
            [
                HumanMessage(content="go"),
                AIMessage(content="", tool_calls=[call]),
                ToolMessage(content=f"big output, see artifact://{artifact}", tool_call_id="c1"),
            ],
        )
        run = run or f"run-{session_id}"
        await self.db.execute(
            "INSERT INTO runs (id, session_id, status) VALUES (?, ?, 'completed')",
            (run, session_id),
        )
        await self.log.append(
            [
                AgentEvent.create(
                    EventType.RUN_STARTED, run_id=run, session_id=session_id, sequence=1
                )
            ]
        )

    async def rows(self, session_id: str) -> int:
        total = 0
        for table in ("sessions", *SESSION_TABLES, "file_changes"):
            column = "id" if table == "sessions" else "session_id"
            found = await self.db.query(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (session_id,)
            )
            total += int(found[0]["n"])
        return total


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


async def test_deleting_a_chat_removes_its_subagent_sessions_but_not_its_branch(
    world: World,
) -> None:
    await world.chat("chat")
    await world.chat("chat::explorer::new")  # P46 layout: nested folder
    old_folder = world.folders / "run-chat::explorer::old"  # pre-P46: flat, run-id prefix
    old_folder.mkdir(parents=True)
    await world.chat("run-chat::explorer::old")
    assert (old_folder / "transcript.jsonl").is_file()
    await world.chat("branch")
    branch = await world.sessions.get("branch")
    assert branch is not None
    branch.branch_of = "chat"
    await world.sessions.save(branch, expected_version=branch.version)

    await world.sessions.delete("chat")

    for gone in ("chat", "chat::explorer::new", "run-chat::explorer::old"):
        assert await world.rows(gone) == 0
    assert not old_folder.exists()
    assert not world.conversation.session_dir("chat").exists()
    assert await world.sessions.get("branch") is not None  # a branch is its own conversation
    assert world.conversation.session_dir("branch").is_dir()


async def test_delete_then_gc_leaves_no_orphan_files_or_rows(world: World) -> None:
    keep = await world.artifacts.put_text(text="x" * 10, name="out.txt")
    drop = await world.artifacts.put_text(text="y" * 10, name="out.txt")
    await world.chat("kept", artifact=keep.id)
    await world.chat("doomed", artifact=drop.id)
    await world.changes.record(session_id="kept", run_id="r", path="a.txt", before=None, after="1")
    await world.changes.record(
        session_id="doomed", run_id="r", path="b.txt", before=None, after="2"
    )
    snapshots = world.folders.parents[1] / "changes"
    stray = snapshots / "nobody-12345678"
    stray.mkdir()
    (stray / "snap").write_text("old")
    (world.artifacts.root / ".tmp-crashed").write_text("half")

    await world.sessions.delete("doomed")
    await world.changes.delete_session("doomed")
    dry = await world.gc.collect(now=LATER)
    report = await world.gc.collect(apply=True, now=LATER)

    assert dry.artifacts == report.artifacts == sorted([drop.id, ".tmp-crashed"])
    assert report.snapshots == ["nobody-12345678"] and report.sessions == []
    assert sorted(p.name for p in world.artifacts.root.iterdir()) == [keep.id]
    assert len(list(snapshots.iterdir())) == 2  # kept's snapshots + the folder-state marker
    assert await world.rows("kept") > 0 and await world.rows("doomed") == 0
    assert (
        (await world.gc.collect(apply=True, now=LATER))
        .text()
        .startswith("Deleted: 0 orphan session(s), 0 artifact(s), 0 change snapshot folder(s)")
    )


async def test_gc_removes_orphan_rows_and_dead_subagents_and_keeps_unindexed_folders(
    world: World,
) -> None:
    await world.chat("chat")
    await world.chat("chat::explorer::alive")  # its chat exists: kept
    await world.chat("vanished::explorer::dead")  # its chat and run are gone: dead
    shutil.rmtree(world.folders / "vanished")  # as a delete before P50 left it: rows only
    await world.log.append(  # rows of a session with no sessions row
        [AgentEvent.create(EventType.RUN_STARTED, run_id="r9", session_id="ghost", sequence=1)]
    )
    await world.conversation.append_messages("foreign", [HumanMessage(content="hi")])  # no rows

    dry = await world.gc.collect(now=LATER)
    assert await world.rows("ghost") > 0  # a dry run deletes nothing
    report = await world.gc.collect(apply=True, now=LATER)

    assert dry.sessions == report.sessions == ["ghost", "vanished::explorer::dead"]
    assert report.unindexed == ["foreign"]
    assert world.conversation.session_dir("foreign").is_dir()  # reported, never deleted
    assert await world.rows("ghost") == await world.rows("vanished::explorer::dead") == 0
    assert await world.rows("chat::explorer::alive") > 0
    assert await world.sessions.get("chat") is not None


async def test_gc_keeps_anything_touched_within_the_grace_period(world: World) -> None:
    fresh = await world.artifacts.put_text(text="z", name="new.txt")
    await world.log.append(
        [AgentEvent.create(EventType.RUN_STARTED, run_id="r1", session_id="live", sequence=1)]
    )

    report = await world.gc.collect(apply=True)  # now: all of it is younger than a day

    assert report.sessions == [] and report.artifacts == []
    assert (world.artifacts.root / fresh.id).is_file()
    assert await world.rows("live") > 0


def test_cli_gc_is_a_dry_run_unless_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_STACK_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.delenv("AGENT_STACK_DB", raising=False)
    old = tmp_path / ".agent" / "artifacts" / "0123456789abcdef0123456789abcdef.txt"
    old.parent.mkdir(parents=True)
    old.write_text("orphan")
    stamp = (datetime.now(UTC) - timedelta(days=3)).timestamp()
    os.utime(old, (stamp, stamp))

    assert cli.main(["gc"]) == 0
    assert "Would delete" in capsys.readouterr().out and old.exists()
    assert cli.main(["gc", "--apply"]) == 0
    assert "Deleted: 0 orphan session(s), 1 artifact(s)" in capsys.readouterr().out
    assert not old.exists()
