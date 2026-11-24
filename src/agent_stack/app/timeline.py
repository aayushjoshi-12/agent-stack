"""Go back, fork, compare and share points in a conversation.

A *turn* is one user message and everything the agent did for it. A *point* is a logged event
(the start or the end of a turn). Everything here is built on three facts:

* ``Application.branch_session`` forks a conversation at a point; the original is never changed.
* Every edit made through ``write_file``/``edit_file`` is recorded with its full before/after text,
  and a fork copies the edits made up to its point (snapshots included). So each conversation
  carries the complete file history that led to it, and no branch loses anything when another is
  deleted or abandoned.
* The working folder is shared, so *which branch's files are on disk* is a separate step:
  :meth:`Timeline.switch_files` moves the folder from one conversation's state to another's and
  refuses to overwrite a file somebody changed by hand (unless ``force``).

Rewinding is therefore "fork at the point, then switch the folder to the fork": the later work
stays in the original conversation, ready to come back to. Files changed by shell commands are not
tracked, so they are neither undone nor restored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

from langchain_core.messages import HumanMessage

from agent_stack.core.changes import ChangeEntry, make_diff
from agent_stack.core.errors import PersistenceError
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.session import Session
from agent_stack.core.workspace import WorkspacePolicy

if TYPE_CHECKING:
    from agent_stack.app.application import Application

__all__ = [
    "REF_PREFIX",
    "FileMove",
    "LineTurn",
    "RewindOutcome",
    "SwitchResult",
    "Timeline",
    "TreeNode",
    "Turn",
    "make_ref",
    "parse_ref",
    "render_ascii",
    "render_mermaid",
]

REF_PREFIX = "asref://"


@dataclass(frozen=True)
class Turn:
    """One user message and the run that answered it, with the points either side of it."""

    index: int  # 1-based, within this conversation's own events
    run_id: str
    started_event_id: str  # a fork here is the conversation as it stood *before* this turn
    completed_event_id: str | None  # a fork here includes the turn; None while it is running
    timestamp: datetime
    user_text: str
    files_changed: int


@dataclass(frozen=True)
class LineTurn:
    """A turn in a conversation's whole lineage: ``session_id`` is the conversation that owns it."""

    turn: Turn
    session_id: str


@dataclass(frozen=True)
class RewindOutcome:
    """What a rewind did: the conversation to be in now, and what happened to the files."""

    session_id: str
    files: SwitchResult
    prompt: str  # the rewound message's text, to put back in the input box


@dataclass(frozen=True)
class FileMove:
    path: str
    action: str  # "wrote" | "deleted"


@dataclass
class SwitchResult:
    moved: list[FileMove] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # changed by hand; left alone

    @property
    def ok(self) -> bool:
        return not self.conflicts


@dataclass
class TreeNode:
    session: Session
    turns: int = 0
    files: list[tuple[str, int, int]] = field(default_factory=list)  # path, added, removed
    point_time: str | None = None  # when its fork point was, for the parent edge
    children: list[TreeNode] = field(default_factory=list)


def make_ref(session_id: str, event_id: str) -> str:
    return f"{REF_PREFIX}{quote(session_id, safe='')}/{event_id}"


def parse_ref(ref: str) -> tuple[str, str]:
    """``(session_id, event_id)`` of a reference made by :meth:`Timeline.reference`."""
    text = ref.strip()
    if not text.startswith(REF_PREFIX) or "/" not in text[len(REF_PREFIX) :]:
        raise PersistenceError(f"not an agent-stack reference: {ref!r}")
    session, _, event = text[len(REF_PREFIX) :].rpartition("/")
    if not session or not event:
        raise PersistenceError(f"not an agent-stack reference: {ref!r}")
    return unquote(session), event


def _as_time(value: str | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _net(entries: list[ChangeEntry]) -> dict[str, tuple[str | None, str]]:
    """Per path: the text before the first edit and after the last one."""
    state: dict[str, tuple[str | None, str]] = {}
    for entry in entries:
        first = state[entry.path][0] if entry.path in state else entry.before
        state[entry.path] = (first, entry.after)
    return state


class Timeline:
    """The history operations of an :class:`Application`."""

    def __init__(self, application: Application) -> None:
        self._app = application

    # ── what is there ────────────────────────────────────────────────────────
    async def turns(self, session_id: str) -> list[Turn]:
        """This conversation's own turns, oldest first (turns before a fork live in its parent)."""
        app = self._app
        events = await app.replay(session_id=session_id)
        top = [e for e in events if e.type is EventType.RUN_STARTED and e.parent_run_id is None]
        done = {e.run_id: e for e in events if e.type is EventType.RUN_COMPLETED}
        texts = await self._user_texts(session_id, len(top))
        entries = await app.change_store.entries(session_id) if app.change_store else []
        found: list[Turn] = []
        for number, started in enumerate(top, 1):
            end = done.get(started.run_id)
            files = {e.path for e in entries if e.run_id == started.run_id}
            found.append(
                Turn(
                    index=number,
                    run_id=started.run_id,
                    started_event_id=started.id,
                    completed_event_id=end.id if end else None,
                    timestamp=started.timestamp,
                    user_text=texts[number - 1] if number - 1 < len(texts) else "",
                    files_changed=len(files),
                )
            )
        return found

    async def lineage(self, session_id: str) -> list[LineTurn]:
        """Every turn that led to this conversation, oldest first, across its forks.

        A fork's earlier turns belong to the conversation it was forked from; each entry names
        its owner, because a point can only be forked from the conversation whose log holds it.
        """
        app = self._app
        session = await app.get_session(session_id)
        own = [LineTurn(turn, session_id) for turn in await self.turns(session_id)]
        if session is None or not session.branch_of or not session.branch_point_event_id:
            return own
        point = await app.event_log.get(session.branch_point_event_id) if app.event_log else None
        if point is None:
            return own
        earlier = [
            lt
            for lt in await self.lineage(session.branch_of)
            if lt.turn.timestamp < point.timestamp
        ]
        return [*earlier, *own]

    async def rewind_to(
        self,
        current: str,
        target: LineTurn,
        *,
        conversation: bool = True,
        code: bool = True,
        force: bool = False,
    ) -> RewindOutcome:
        """Go back to just before ``target``'s message, as Claude Code's rewind does.

        ``conversation``: fork there and continue in the fork (the original keeps everything).
        ``code``: put the files as they were then. With only ``code``, the conversation stays and
        the restore is recorded in it as ordinary edits, so its file history stays truthful.
        The returned ``prompt`` is the rewound message, to be offered for editing and resending.
        """
        app = self._app
        event = target.turn.started_event_id
        prompt = target.turn.user_text
        if conversation:
            branch = await self.fork(target.session_id, event)
            moved = (
                await self.switch_files(current, branch, force=force) if code else SwitchResult()
            )
            return RewindOutcome(branch, moved, prompt)
        if not code or app.change_store is None:
            return RewindOutcome(current, SwitchResult(), prompt)
        stamp = (await self.event(target.session_id, event)).timestamp
        wanted = _net(
            [
                e
                for e in await app.change_store.entries(target.session_id)
                if _as_time(e.created_at) <= stamp
            ]
        )
        have = _net(await app.change_store.entries(current))
        policy = WorkspacePolicy(app.config.workspace)
        result = SwitchResult()
        for path in sorted(set(have) | set(wanted)):
            pristine = (have.get(path) or wanted[path])[0]
            expected = have[path][1] if path in have else pristine
            desired = wanted[path][1] if path in wanted else pristine
            if expected == desired:
                continue
            file = policy.resolve(path)
            actual = file.read_text(encoding="utf-8", errors="replace") if file.is_file() else None
            if actual != expected and not force:
                result.conflicts.append(path)
                continue
            if desired is None:
                file.unlink(missing_ok=True)
                result.moved.append(FileMove(path, "deleted"))
                continue
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(desired, encoding="utf-8")
            result.moved.append(FileMove(path, "wrote"))
            await app.change_store.record(
                session_id=current, run_id="rewind", path=path, before=actual, after=desired
            )
        return RewindOutcome(current, result, prompt)

    async def _user_texts(self, session_id: str, count: int) -> list[str]:
        """The last ``count`` user messages: a fork's own turns are the tail of its transcript."""
        messages = await self._app.session_store.get_messages(session_id)
        humans = [str(m.content) for m in messages if isinstance(m, HumanMessage)]
        return humans[len(humans) - count :] if count else []

    async def event(self, session_id: str, point: str) -> AgentEvent:
        """Resolve an event id, or a turn number (``3`` = after turn 3, ``3-`` = before it)."""
        text = point.strip()
        number = text.rstrip("-")
        if number.isdigit():
            turns = await self.turns(session_id)
            index = int(number)
            if not 1 <= index <= len(turns):
                raise PersistenceError(f"{session_id} has {len(turns)} turn(s), not {index}")
            turn = turns[index - 1]
            wanted = turn.started_event_id if text.endswith("-") else turn.completed_event_id
            if wanted is None:
                raise PersistenceError(f"turn {index} has not finished; use '{index}-'")
            text = wanted
        event = await self._app.event_log.get(text) if self._app.event_log else None
        if event is None or event.session_id != session_id:
            raise PersistenceError(f"event {text} is not in session {session_id}")
        return event

    # ── files ────────────────────────────────────────────────────────────────
    async def folder_state(self) -> str | None:
        """The conversation whose files the working folder currently holds, if known."""
        store = self._app.change_store
        return None if store is None else await store.folder_state()

    async def files_differ(self, session_id: str) -> bool:
        """True when putting the folder in ``session_id``'s state would change something."""
        source = await self.folder_state()
        if source is None or source == session_id:
            return False
        result = await self.switch_files(source, session_id, dry_run=True)
        return bool(result.moved or result.conflicts)

    async def restore_files(self, session_id: str, *, force: bool = False) -> SwitchResult:
        """Put the folder in ``session_id``'s file state (from whatever it holds now)."""
        source = await self.folder_state() or session_id
        return await self.switch_files(source, session_id, force=force)

    async def switch_files(
        self, from_session: str, to_session: str, *, force: bool = False, dry_run: bool = False
    ) -> SwitchResult:
        """Make the working folder match ``to_session`` after it matched ``from_session``.

        Only files either conversation edited are touched. A file whose current content is not
        what ``from_session`` left (somebody edited it by hand) is reported as a conflict and left
        alone, unless ``force``. With ``dry_run`` nothing is written; the result says what would be.
        """
        app = self._app
        if app.change_store is None:
            raise PersistenceError("this application does not record file changes")
        source = _net(await app.change_store.entries(from_session))
        target = _net(await app.change_store.entries(to_session))
        policy = WorkspacePolicy(app.config.workspace)
        result = SwitchResult()
        for path in sorted(set(source) | set(target)):
            pristine = (source.get(path) or target[path])[0]
            expected = source[path][1] if path in source else pristine
            desired = target[path][1] if path in target else pristine
            if expected == desired:
                continue
            file = policy.resolve(path)
            actual = file.read_text(encoding="utf-8", errors="replace") if file.is_file() else None
            if actual != expected and not force:
                result.conflicts.append(path)
                continue
            if desired is None:
                if not dry_run:
                    file.unlink(missing_ok=True)
                result.moved.append(FileMove(path, "deleted"))
            else:
                if not dry_run:
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(desired, encoding="utf-8")
                result.moved.append(FileMove(path, "wrote"))
        if not dry_run and not result.conflicts:
            await app.change_store.set_folder_state(to_session)
        return result

    # ── go back / fork ───────────────────────────────────────────────────────
    async def fork(self, session_id: str, point: str) -> str:
        """Fork at a point (event id or turn number) with its file history; no file moves."""
        app = self._app
        event = await self.event(session_id, point)
        branch_id = await app.branch_session(session_id, event.id)
        if app.change_store is not None:
            await app.change_store.copy_upto(session_id, branch_id, event.timestamp.isoformat())
        return branch_id

    async def rewind(
        self, session_id: str, point: str, *, restore_files: bool = True, force: bool = False
    ) -> tuple[str, SwitchResult]:
        """Go back to a point: fork there and (by default) put the files as they were then.

        Returns the new conversation and what happened to the files. The original conversation
        keeps everything that came after the point.
        """
        branch_id = await self.fork(session_id, point)
        if not restore_files:
            return branch_id, SwitchResult()
        return branch_id, await self.switch_files(session_id, branch_id, force=force)

    # ── the graph ────────────────────────────────────────────────────────────
    async def tree(self, session_id: str) -> TreeNode:
        """The whole family of branches that ``session_id`` belongs to, rooted at the original."""
        app = self._app
        store = app.session_store
        if not hasattr(store, "list_all"):
            raise PersistenceError("this session store cannot list every conversation")
        sessions = {s.id: s for s in await store.list_all(2000)}
        if session_id not in sessions:
            raise PersistenceError(f"unknown session {session_id}")
        root = sessions[session_id]
        while root.branch_of and root.branch_of in sessions:
            root = sessions[root.branch_of]
        children: dict[str, list[Session]] = {}
        for session in sessions.values():
            if session.branch_of:
                children.setdefault(session.branch_of, []).append(session)

        async def build(session: Session) -> TreeNode:
            node = TreeNode(session=session, turns=len(await self.turns(session.id)))
            if app.change_store is not None:
                for path in await app.change_store.paths(session.id):
                    diff = await app.change_store.file_diff(session.id, path)
                    if diff is not None:
                        node.files.append((path, diff.added, diff.removed))
            if session.branch_point_event_id and app.event_log is not None:
                point = await app.event_log.get(session.branch_point_event_id)
                node.point_time = point.timestamp.isoformat(timespec="seconds") if point else None
            for child in sorted(children.get(session.id, []), key=lambda s: s.id):
                node.children.append(await build(child))
            return node

        return await build(root)

    # ── references ───────────────────────────────────────────────────────────
    async def reference(self, session_id: str, point: str) -> str:
        """A string that names a point in a conversation, to paste into another chat."""
        event = await self.event(session_id, point)
        return make_ref(session_id, event.id)

    async def describe(self, ref: str, *, max_chars: int = 12_000) -> str:
        """What happened up to the referenced point, readable by an agent or a person.

        The conversation (user messages and the agent's replies, tool calls abbreviated) and the
        files as they stood then, each with its diff against where it started.
        """
        app = self._app
        session_id, event_id = parse_ref(ref)
        event = await app.event_log.get(event_id) if app.event_log else None
        session = await app.get_session(session_id)
        if event is None or session is None or event.session_id != session_id:
            raise PersistenceError(f"cannot find {ref}")
        store = app.session_store
        records = await store.records(session_id) if hasattr(store, "records") else []
        lines = [f"Reference {ref}", f"Conversation: {session.title or session_id}"]
        if session.branch_of:
            lines.append(f"Forked from {session.branch_of}")
        lines += ["", "## Conversation up to that point"]
        for record in records:
            if record.ts > event.timestamp:
                continue
            if record.kind == "human":
                lines.append(f"USER: {record.content.strip()}")
            elif record.kind == "ai":
                calls = ", ".join(str(c.get("name")) for c in record.tool_calls)
                text = record.content.strip()
                if text or calls:
                    lines.append(f"AGENT: {text}" + (f"  [tools: {calls}]" if calls else ""))
        entries = [
            e
            for e in (await app.change_store.entries(session_id) if app.change_store else [])
            if _as_time(e.created_at) <= event.timestamp
        ]
        lines += ["", "## Files changed up to that point"]
        if not entries:
            lines.append("(none recorded)")
        for path, (before, after) in sorted(_net(entries).items()):
            diff, added, removed, _ = make_diff(path, before, after)
            kind = "created" if before is None else "modified"
            lines += [f"### {path} ({kind}, +{added} -{removed})", "```diff", diff, "```"]
        lines += [
            "",
            "To continue from here: open it as a new conversation (`agent-stack ref open`), "
            "then put its files in the working folder with `agent-stack switch`.",
        ]
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[:max_chars] + "\n…[truncated]"

    async def open_reference(self, ref: str) -> str:
        """A new conversation that continues from the referenced point (files not yet placed)."""
        session_id, event_id = parse_ref(ref)
        return await self.fork(session_id, event_id)


# ── drawing ──────────────────────────────────────────────────────────────────
def _label(node: TreeNode) -> str:
    title = (node.session.title or "untitled").replace('"', "'")[:40]
    return f"{node.session.id}  {title}"


def render_ascii(root: TreeNode, *, current: str | None = None, files: bool = True) -> str:
    """The branch tree as text; ``*`` marks the conversation you are in."""
    out: list[str] = []

    def line(node: TreeNode) -> str:
        mark = "*" if node.session.id == current else "o"
        stats = f"{node.turns} turn{'s' if node.turns != 1 else ''}, {len(node.files)} file(s)"
        return f"{mark} {_label(node)}  ({stats})"

    def walk(node: TreeNode, prefix: str, last: bool, top: bool) -> None:
        branch = "" if top else ("└─ " if last else "├─ ")
        out.append(prefix + branch + line(node))
        pad = prefix + ("" if top else ("   " if last else "│  "))
        if node.point_time:
            out.append(pad + f"  forked at {node.point_time}")
        if files:
            for path, added, removed in node.files:
                out.append(pad + f"  · {path} +{added} -{removed}")
        for i, child in enumerate(node.children):
            walk(child, pad, i == len(node.children) - 1, False)

    walk(root, "", True, True)
    return "\n".join(out)


def render_mermaid(root: TreeNode, *, current: str | None = None) -> str:
    """The branch tree as a Mermaid flowchart (the web UI and most markdown viewers draw it)."""
    out = ["flowchart TD"]
    ids: dict[str, str] = {}

    def node_id(node: TreeNode) -> str:
        return ids.setdefault(node.session.id, f"n{len(ids)}")

    def walk(node: TreeNode, parent: TreeNode | None) -> None:
        name = node_id(node)
        files = f"{len(node.files)} files" if node.files else "no files"
        text = f"{_label(node)}<br/>{node.turns} turns, {files}".replace('"', "'")
        out.append(f'  {name}["{text}"]')
        if node.session.id == current:
            out.append(f"  style {name} stroke-width:3px")
        if parent is not None:
            out.append(f"  {node_id(parent)} -->|{node.point_time or 'fork'}| {name}")
        for child in node.children:
            walk(child, node)

    walk(root, None)
    return "\n".join(out)
