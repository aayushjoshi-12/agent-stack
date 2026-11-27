"""Per-run context that tools read through a :class:`ContextVar`.

Delegation and the parent-side control tools need to act on *the current run* without
importing the application or the run manager. ``RunManager`` sets a frozen
:class:`RunContext` around the runtime call; tools read it via
:func:`current_run_context`. ``services`` is the minimal protocol they use to
start/observe/cancel/inject runs — satisfied by ``RunManager`` itself. It deliberately
imports nothing from ``core.run`` so there is no import cycle (``core.run`` imports this
module).
"""

from __future__ import annotations

from collections.abc import Awaitable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from langchain_core.messages import AIMessage

from agent_stack.core.agent import Agent

__all__ = [
    "RunContext",
    "RunServices",
    "RunView",
    "SkillScope",
    "SubagentRun",
    "current_run_context",
    "run_context",
]


@dataclass(frozen=True)
class SubagentRun:
    """A started child run: its id and an awaitable for its final message."""

    run_id: str
    result: Awaitable[AIMessage]


@dataclass(frozen=True)
class RunView:
    """A read-only snapshot of a run for the parent's control tools."""

    run_id: str
    status: str
    steps: int = 0
    tool_calls: int = 0
    last_event: str | None = None
    error: str | None = None
    parent_run_id: str | None = None


@runtime_checkable
class RunServices(Protocol):
    """The run operations tools use; satisfied by :class:`RunManager`."""

    async def start_subagent(
        self,
        agent: Agent,
        *,
        parent_run_id: str,
        user_id: str,
        message: str,
        depth: int,
    ) -> SubagentRun: ...

    async def get_run_view(self, run_id: str) -> RunView | None: ...

    async def cancel(self, run_id: str) -> None: ...

    async def send_input(self, run_id: str, message: str) -> bool: ...

    async def list_child_ids(self, parent_run_id: str) -> list[str]: ...


@dataclass
class SkillScope:
    """The tools a run may still call, narrowed by each skill it loads (P49).

    ``allowed`` is ``None`` until a skill with ``allowed-tools`` is loaded; each such skill
    intersects it, so loading a skill never widens what the run may do.
    """

    allowed: frozenset[str] | None = None
    skills: tuple[str, ...] = ()

    def narrow(self, skill: str, tools: Iterable[str]) -> None:
        names = frozenset(tools)
        self.allowed = names if self.allowed is None else self.allowed & names
        self.skills = (*self.skills, skill)

    def allows(self, tool: str) -> bool:
        return self.allowed is None or tool in self.allowed


@dataclass(frozen=True)
class RunContext:
    """The current run's identity plus the services its tools may call."""

    run_id: str
    session_id: str
    user_id: str
    depth: int
    parent_run_id: str | None
    agent: Agent
    services: RunServices
    skill_scope: SkillScope = field(default_factory=SkillScope)


_current: ContextVar[RunContext | None] = ContextVar("agent_stack_run_context", default=None)


def current_run_context() -> RunContext | None:
    """The context of the run executing in this task, or ``None`` outside a run."""
    return _current.get()


@contextmanager
def run_context(context: RunContext) -> Iterator[None]:
    """Make ``context`` current for the duration of the ``with`` block."""
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)
