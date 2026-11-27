"""P49: a skill's ``allowed-tools`` and a subagent's tool set only narrow what a run may do."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_stack.app.bootstrap import build_application
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent, SubAgentSpec
from agent_stack.core.approval import ApprovalBroker, ToolPolicy
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.run import RunManager, RunRequest
from agent_stack.core.run_context import SkillScope
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.skills import SkillLoader, SkillRegistry, skill_tools
from agent_stack.subagents.delegation import delegate, inherit
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call


class CountingTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "Counts its runs."
        self.runs = 0

    async def ainvoke(self, args: dict[str, Any]) -> str:
        self.runs += 1
        return f"{self.name} ran"


class DecidingBus(InMemoryEventBus):
    """Remembers every approval request and, unless ``approve`` is None, answers it at once."""

    def __init__(self, broker: ApprovalBroker, *, approve: bool | None) -> None:
        super().__init__()
        self.broker = broker
        self.approve = approve
        self.asked: list[AgentEvent] = []

    async def publish(self, event: AgentEvent) -> None:
        if event.type is EventType.APPROVAL_REQUIRED:
            self.asked.append(event)
        if event.type is EventType.APPROVAL_REQUIRED and self.approve is not None:
            self.broker.decide(
                str(event.data["approval_id"]),
                run_id=event.run_id,
                session_id=event.session_id,
                principal="u1",
                approve=self.approve,
            )
        await super().publish(event)


def _skill(root: Path, name: str, allowed: str | None) -> None:
    (root / name).mkdir(parents=True)
    line = f"allowed-tools: {allowed}\n" if allowed is not None else ""
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n{line}---\nDo the {name} thing.\n"
    )


def test_allowed_tools_parses_commas_lists_and_empty(tmp_path: Path) -> None:
    _skill(tmp_path, "commas", "read_file, grep_files")
    _skill(tmp_path, "listed", "[read_file]")
    _skill(tmp_path, "nothing", "[]")
    _skill(tmp_path, "free", None)
    by_name = {m.name: m.allowed_tools for m in SkillLoader([tmp_path]).discover()}
    assert by_name == {
        "commas": ("read_file", "grep_files"),
        "listed": ("read_file",),
        "nothing": (),  # an explicit empty list allows no tool, not every tool
        "free": None,
    }


def test_skill_scopes_intersect() -> None:
    scope = SkillScope()
    assert scope.allows("anything")
    scope.narrow("a", ["read_file", "grep_files"])
    scope.narrow("b", ["read_file", "run_command"])
    assert scope.allows("read_file")
    assert not scope.allows("grep_files") and not scope.allows("run_command")
    assert scope.skills == ("a", "b")


async def test_a_loaded_skill_blocks_every_tool_it_does_not_allow(tmp_path: Path) -> None:
    _skill(tmp_path, "reader", "safe")
    safe, danger = CountingTool("safe"), CountingTool("danger")
    tools = (*skill_tools(SkillRegistry(SkillLoader([tmp_path]))), safe, danger)
    model = FakeChatModel(
        [
            ai(tool_calls=[call("load_skill", {"name": "reader"})]),
            ai(tool_calls=[call("safe", id="c2"), call("danger", id="c3")]),
            ai("done"),
        ]
    )
    broker = ApprovalBroker()
    bus = DecidingBus(broker, approve=True)
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tools,  # type: ignore[arg-type]
        tool_policy=ToolPolicy(frozenset({"danger"})),
    )
    runtime = AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(list(tools))),  # type: ignore[arg-type]
        session_store=InMemorySessionStore(),
        event_bus=bus,
        approvals=broker,
    )
    manager = RunManager(runtime=runtime, run_store=InMemoryRunStore(), event_bus=bus)

    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    assert (await handle.result()).content == "done"

    assert (safe.runs, danger.runs) == (1, 0)
    assert bus.asked == []  # blocked outright, never offered for approval
    seen = [str(m.content) for m in model.calls[2]]
    assert any("may call only: load_skill, read_skill_resource, safe" in text for text in seen)
    assert any("skill reader is active and allows only" in text for text in seen)


def test_inherit_cuts_tools_to_the_parent_and_unions_the_approvals() -> None:
    read, write, extra = CountingTool("read"), CountingTool("write"), CountingTool("extra")
    outside = ToolPolicy(dynamic=lambda tool, args: args.get("path") == "/etc/passwd")
    parent = Agent(
        name="assistant",
        model="m",
        tools=(read, write),  # type: ignore[arg-type]
        tool_policy=ToolPolicy(frozenset({"write"})).union(outside),
    )
    child = Agent(name="coder", model="m", tools=(read, write, extra))  # type: ignore[arg-type]

    narrowed = inherit(child, parent, SkillScope())
    assert [t.name for t in narrowed.tools] == ["read", "write"]
    assert narrowed.tool_policy.needs_approval("write", {})
    assert narrowed.tool_policy.needs_approval("read", {"path": "/etc/passwd"})
    assert not narrowed.tool_policy.needs_approval("read", {"path": "a.txt"})

    scope = SkillScope()
    scope.narrow("reader", ["read"])
    assert [t.name for t in inherit(child, parent, scope).tools] == ["read"]


@pytest.mark.parametrize("approve", [True, False])
async def test_a_subagent_write_goes_through_the_parents_approval(approve: bool) -> None:
    write = CountingTool("write")
    broker = ApprovalBroker()
    bus = DecidingBus(broker, approve=None)  # answered from the parent's stream, as the CLI does
    store = InMemorySessionStore()
    coder = Agent(name="coder", model="m", tools=(write,))  # type: ignore[arg-type]  # no policy
    parent = Agent(
        name="assistant",
        model="m",
        tools=(delegate, write),  # type: ignore[arg-type]
        subagents=(SubAgentSpec("coder", "writes", coder),),
        tool_policy=ToolPolicy(frozenset({"write"})),
    )
    child_model = FakeChatModel([ai(tool_calls=[call("write")]), ai("child done")])
    created: list[Agent] = []

    def runtime(agent: Agent, model: Any) -> AgentRuntime:
        return AgentRuntime(
            agent=agent,
            model=model,
            tool_executor=ToolExecutor(ToolRegistry(list(agent.tools))),
            session_store=store,
            event_bus=bus,
            approvals=broker,
        )

    class Factory:
        def create(self, agent: Agent) -> AgentRuntime:
            created.append(agent)
            return runtime(agent, child_model)

    parent_model = FakeChatModel(
        [ai(tool_calls=[call("delegate", {"agent_name": "coder", "task": "edit"})]), ai("done")]
    )
    manager = RunManager(
        runtime=runtime(parent, parent_model),
        run_store=InMemoryRunStore(),
        event_bus=bus,
        agent_factory=Factory(),
    )

    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    # The child's question reaches the top-level stream (all the CLI, TUI and Telegram read)
    # with the child's ids, which are what the answer must carry.
    stream = []
    async for event in handle.events():
        stream.append(event)
        if event.type is EventType.APPROVAL_REQUIRED:
            broker.decide(
                str(event.data["approval_id"]),
                run_id=event.run_id,
                session_id=event.session_id,
                principal="u1",
                approve=approve,
            )
    assert (await handle.result()).content == "done"

    (asked,) = bus.asked
    assert asked.data["tool"] == "write" and asked.parent_run_id == handle.run_id
    assert asked.run_id != handle.run_id
    assert any(e.type is EventType.APPROVAL_RESOLVED for e in stream)
    assert created[0].tool_policy.needs_approval("write", {})
    assert write.runs == (1 if approve else 0)


def test_coder_is_registered_within_the_parents_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    app = build_application(AppConfig(workspace=tmp_path / "ws", db_path=tmp_path / "i.db"))
    coder = next(s.agent for s in app.agent.subagents if s.name == "coder")
    names = {t.name for t in coder.tools}
    assert {"read_file", "write_file", "edit_file", "run_command"} <= names
    assert names <= {t.name for t in app.agent.tools}
    assert coder.model == app.agent.model
