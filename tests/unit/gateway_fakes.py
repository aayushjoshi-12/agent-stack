"""Duck-typed gateway doubles: a fake Application and run handles with canned events.

Tests are not type-checked (mypy runs on ``src`` only), so these favour clarity. No
real model, store, or event bus is touched.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from langchain_core.messages import AIMessage

from agent_stack.app.application import FileDiff
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import HistoryItem, SessionSummary, SessionUsage

_counter = itertools.count()


def event(
    etype: EventType,
    *,
    run_id: str = "run-1",
    session_id: str = "s1",
    parent_run_id: str | None = None,
    **data: Any,
) -> AgentEvent:
    """Build a deterministic AgentEvent with an incrementing id."""
    return AgentEvent(
        id=f"ev-{next(_counter)}",
        type=etype,
        run_id=run_id,
        session_id=session_id,
        sequence=next(_counter),
        timestamp=datetime.now(UTC),
        data=dict(data),
        parent_run_id=parent_run_id,
    )


@dataclass
class FakeConfig:
    workspace: Path = Path(".")
    model: str = "test:model"
    theme: str = "agent-stack-dark"
    context_window: int = 200_000
    budget_usd: float | None = None
    rate_messages: int | None = 30
    rate_requests: int | None = 600


class FakeAgent:
    name = "assistant"


class FakeSessionStore:
    """Only the surface ``SessionResolver`` needs."""

    def __init__(self) -> None:
        self.saved: list[str] = []

    async def get(self, session_id: str) -> None:
        return None

    async def save(self, session: Any, *, expected_version: int | None = None) -> None:
        self.saved.append(session.id)


class FakeHandle:
    """A run handle whose ``events()`` yields canned events (then optionally blocks)."""

    def __init__(
        self,
        run_id: str,
        events: Sequence[AgentEvent],
        gate: Any = None,
    ) -> None:
        self.run_id = run_id
        self._events = list(events)
        self._gate = gate

    async def events(self) -> AsyncIterator[AgentEvent]:
        for item in self._events:
            yield item
        if self._gate is not None:
            await self._gate.wait()

    async def result(self) -> AIMessage:
        return AIMessage(content="done")


class FakeApplication:
    """A duck-typed Application: records calls, returns canned event streams."""

    def get_approval_mode(self) -> str:
        return self.approval_mode

    def set_approval_mode(self, mode: str) -> str:
        if mode not in ("ask", "auto"):
            raise ValueError("approval mode must be ask or auto")
        self.approval_mode = mode
        return mode

    def get_model(self) -> str:
        return getattr(self, "model", "test:model")

    def set_model(self, name: str) -> str:
        if ":" not in name:
            raise ValueError(f"Failed to build model {name!r}")
        self.model = name
        return name

    async def compact_session(self, session_id: str, focus: str | None = None) -> int:
        self.compacted = [*getattr(self, "compacted", []), (session_id, focus)]
        return 7

    def slash_commands(self) -> list[Any]:
        return list(self.skill_commands)

    def __init__(
        self,
        *,
        events: Sequence[AgentEvent] = (),
        child_events: Mapping[str, Sequence[AgentEvent]] | None = None,
        gate: Any = None,
        workspace: Path | None = None,
    ) -> None:
        self.config = FakeConfig(workspace=workspace or Path("."))
        self.agent = FakeAgent()
        self.session_store = FakeSessionStore()
        self._events = list(events)
        self._child_events = dict(child_events or {})
        self._gate = gate
        self.sent: list[tuple[Any, str, str | None]] = []
        self.cancelled: list[str] = []
        self.inputs: list[tuple[str, str]] = []
        self.withdrawn: list[tuple[str, str]] = []
        self.withdraw_ok = True  # False: the run already read it
        self._run_counter = 0
        self._session_counter = 0
        self.sessions: list[SessionSummary] = []
        self.titles: dict[str, str] = {}
        self.deleted: list[str] = []
        self.decisions: list[tuple[str, str, str, str, bool]] = []
        self.histories: dict[str, list[HistoryItem]] = {}
        self.usages: dict[str, list[SessionUsage]] = {}  # per session: successive answers
        self.file_diffs: dict[str, list[FileDiff]] = {}  # session -> net diff per changed file
        self.latest_diffs: dict[tuple[str, str], FileDiff] = {}  # (session, path) -> last edit
        self.approval_mode = "ask"
        self.skill_commands: list[Any] = []
        self.diff_lookups: list[tuple[str, str, bool]] = []

    async def send_message(self, envelope: Any, *, parent_run_id: str | None = None) -> FakeHandle:
        self._run_counter += 1
        run_id = f"run-{self._run_counter}"
        self.sent.append((envelope, run_id, parent_run_id))
        return FakeHandle(run_id, self._events, self._gate)

    def decide_approval(
        self, approval_id: str, *, run_id: str, session_id: str, principal: str, approve: bool
    ) -> None:
        self.decisions.append((approval_id, run_id, session_id, principal, approve))

    async def cancel_run(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def send_input(self, run_id: str, message: str) -> bool:
        self.inputs.append((run_id, message))
        return True

    async def withdraw_input(self, run_id: str, message: str) -> bool:
        self.withdrawn.append((run_id, message))
        return self.withdraw_ok

    async def get_run(self, run_id: str) -> None:
        return None

    async def child_runs(self, parent_run_id: str) -> list[Any]:
        return []

    async def get_session(self, session_id: str) -> Any:
        found = next((s for s in self.sessions if s.id == session_id), None)
        return None if found is None else SimpleNamespace(id=found.id, title=found.title)

    # -- conversations (new / list / title / history) ----------------------
    async def new_session(self, principal: str) -> str:
        self._session_counter += 1
        session_id = f"{principal}-new{self._session_counter}"
        self.sessions.insert(0, SessionSummary(id=session_id, title="Untitled"))
        return session_id

    async def list_sessions(self, principal: str, limit: int = 30) -> list[SessionSummary]:
        return list(self.sessions)[:limit]

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)
        self.sessions = [s for s in self.sessions if s.id != session_id]

    async def rename_session(self, session_id: str, title: str) -> None:
        self.titles[session_id] = title
        self.sessions = [
            SessionSummary(id=s.id, title=title) if s.id == session_id else s for s in self.sessions
        ]

    async def set_session_title(self, session_id: str, title: str) -> None:
        self.titles[session_id] = title
        self.sessions = [
            SessionSummary(id=s.id, title=title) if s.id == session_id else s for s in self.sessions
        ]

    async def changed_files(self, session_id: str, run_id: str | None = None) -> list[FileDiff]:
        return list(self.file_diffs.get(session_id, ()))

    async def changed_file(
        self, session_id: str, path: str, run_id: str | None = None, *, latest_only: bool = False
    ) -> FileDiff | None:
        self.diff_lookups.append((session_id, path, latest_only))
        if latest_only:
            return self.latest_diffs.get((session_id, path))
        return next((f for f in self.file_diffs.get(session_id, ()) if f.path == path), None)

    async def session_usage(self, session_id: str) -> SessionUsage:
        """The scripted totals for a session; successive calls walk through the list."""
        queue = self.usages.get(session_id)
        if not queue:
            return SessionUsage()
        return queue.pop(0) if len(queue) > 1 else queue[0]

    async def session_history(self, session_id: str) -> list[HistoryItem]:
        return list(self.histories.get(session_id, ()))

    def subscribe(self, run_id: str) -> AsyncIterator[AgentEvent]:
        canned = list(self._child_events.get(run_id, ()))

        async def stream() -> AsyncIterator[AgentEvent]:
            for item in canned:
                yield item

        return stream()
