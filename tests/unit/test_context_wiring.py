"""Acceptance tests for Phase 16e — context wiring reaches the model.

These drive the real ``AgentRuntime`` with a deterministic ``FakeChatModel`` and assert
on the exact message list the model received: resident system parts (instructions, skill
metadata) and the bounded history window. No network, no real LLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_stack.context.composer import SystemPromptComposer
from agent_stack.context.instructions import InstructionsLoader
from agent_stack.core.agent import Agent
from agent_stack.core.context import (
    COMPACT_PROMPT,
    COMPACTION_HEADER,
    FILES_HEADER,
    ContextBuilder,
)
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.memory import MemoryIndex
from agent_stack.skills import SkillLoader, SkillRegistry
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai


class RecordingBus(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[AgentEvent] = []

    async def publish(self, event: AgentEvent) -> None:
        self.published.append(event)
        await super().publish(event)


def _runtime(
    model: Any,
    store: InMemorySessionStore,
    *,
    context_builder: ContextBuilder | None = None,
    system_parts: SystemPromptComposer | None = None,
) -> AgentRuntime:
    agent = Agent(name="assistant", model="test:model", system_prompt="BASE PROMPT")
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(())),
        session_store=store,
        context_builder=context_builder,
        system_parts=system_parts,
    )


def _composer(workspace: Path) -> SystemPromptComposer:
    return SystemPromptComposer(
        instructions=InstructionsLoader(),
        skills=SkillRegistry(SkillLoader([workspace / ".agent" / "skills"])),
        memory=MemoryIndex(workspace),
        workspace_root=workspace,
    )


async def test_agents_md_reaches_model_prompt(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(
        "Project convention: always answer with the word BANANA.", encoding="utf-8"
    )

    model = FakeChatModel([ai("ok")])
    runtime = _runtime(
        model,
        InMemorySessionStore(),
        context_builder=ContextBuilder(),
        system_parts=_composer(workspace),
    )

    await runtime.run(session_id="s1", user_id="u1", message="hello")

    system = model.calls[0][0]
    assert isinstance(system, SystemMessage)
    assert "BANANA" in system.content
    # Base prompt first, project instructions after.
    assert system.content.index("BASE PROMPT") < system.content.index("BANANA")


async def test_skill_metadata_reaches_model_prompt(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skill_dir = workspace / ".agent" / "skills" / "good-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: good-skill\ndescription: Does the good thing.\n---\n\nSECRET-BODY-42",
        encoding="utf-8",
    )

    model = FakeChatModel([ai("ok")])
    runtime = _runtime(
        model,
        InMemorySessionStore(),
        context_builder=ContextBuilder(),
        system_parts=_composer(workspace),
    )

    await runtime.run(session_id="s1", user_id="u1", message="hello")

    system = model.calls[0][0]
    assert isinstance(system, SystemMessage)
    # Level 1 is resident: name + description only.
    assert "good-skill" in system.content
    assert "Does the good thing." in system.content
    # Level 2 (the body) is not — it must be fetched via load_skill.
    assert "SECRET-BODY-42" not in system.content


async def test_history_window_caps_model_input() -> None:
    store = InMemorySessionStore()
    await store.append_messages("s1", [HumanMessage(content=f"h{i}") for i in range(10)])

    model = FakeChatModel([ai("ok")])
    runtime = _runtime(model, store, context_builder=ContextBuilder(recent_window=4))

    await runtime.run(session_id="s1", user_id="u1", message="new")

    sent = model.calls[0]
    assert isinstance(sent[0], SystemMessage)
    messages = sent[1:]
    # The window (4), not the whole history (10 + the new message).
    assert len(messages) == 4
    assert messages[-1].content == "new"
    assert messages[0].content == "h7"
    assert "h0" not in [message.content for message in messages]


def _big_history(count: int = 30) -> list[BaseMessage]:
    read = AIMessage(
        content="", tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "r1"}]
    )
    chat = [HumanMessage(content=f"h{i} " + "x" * 400) for i in range(count)]
    return [*chat, read, ToolMessage(content="print('old')", tool_call_id="r1", name="read_file")]


def _compacting_runtime(
    model: Any, store: InMemorySessionStore, bus: InMemoryEventBus | None = None, **kw: Any
) -> AgentRuntime:
    agent = Agent(name="assistant", model="test:model", system_prompt="BASE PROMPT")
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(())),
        session_store=store,
        context_builder=ContextBuilder(recent_window=1000, max_tokens=2000),
        event_bus=bus,
        **kw,
    )


async def test_past_the_threshold_the_own_model_summarises_and_the_summary_is_saved(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("print('now')\n")
    store = InMemorySessionStore()
    await store.append_messages("s1", _big_history())  # ~3k tokens against a 2k budget
    bus = RecordingBus()
    model = FakeChatModel([ai("SUMMARY"), ai("first"), ai("second")])
    runtime = _compacting_runtime(model, store, bus, workspace_root=tmp_path)

    await runtime.run(session_id="s1", user_id="u1", message="new")

    # the compaction call: what the model was about to see, with the instructions at the end
    *seen, ask = model.calls[0]
    assert str(ask.content).startswith(COMPACT_PROMPT)
    assert seen[-1].content == "new" and "BASE PROMPT" in str(seen[0].content)
    # the next call sees the summary, the request and the file as it is now, not the history
    system, *sent = model.calls[1]
    assert "SUMMARY" in str(system.content) and str(system.content).startswith("BASE PROMPT")
    assert FILES_HEADER not in str(system.content)
    assert sent[0].content == "new"
    assert FILES_HEADER in str(sent[1].content) and "print('now')" in str(sent[1].content)
    assert len(sent) == 2
    compacted = [e for e in bus.published if e.type == EventType.CONTEXT_COMPACTED]
    assert compacted and compacted[0].data == {"messages": 33, "auto": True}

    # the marker is saved: the next run starts from it without summarising again
    stored = await store.get_messages("s1")
    assert len(stored) == 32 + 3  # nothing is deleted from the transcript
    await runtime.run(session_id="s1", user_id="u1", message="again")
    assert len(model.calls) == 3
    assert [m.content for m in model.calls[2][2:]] == ["first", "again"]


async def test_a_failed_summary_falls_back_to_dropping_and_is_not_retried() -> None:
    class FailingSummaries(FakeChatModel):
        async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
            if str(messages[-1].content).startswith(COMPACT_PROMPT):
                self.calls.append(list(messages))
                raise RuntimeError("provider is down")
            return await super().ainvoke(messages, **kwargs)

    store = InMemorySessionStore()
    await store.append_messages("s1", _big_history())
    model = FailingSummaries([ai("ok")])
    runtime = _compacting_runtime(model, store)

    result = await runtime.run(session_id="s1", user_id="u1", message="new")

    assert result.content == "ok"
    assert len(model.calls) == 2  # one failed summary, then the answer: no retry
    assert "earlier messages were dropped" in str(model.calls[1][1].content)


async def test_compact_now_uses_the_focus_records_its_cost_and_runs_once() -> None:
    store = InMemorySessionStore()
    await store.append_messages("s1", [HumanMessage(content="hi"), AIMessage(content="hello")])
    bus = RecordingBus()
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    model = FakeChatModel([AIMessage(content="SUM", usage_metadata=usage)])  # type: ignore[arg-type]
    runtime = _compacting_runtime(model, store, bus)

    assert await runtime.compact("s1", "the API design") == 2
    assert "focus on: the API design" in str(model.calls[0][-1].content)
    assert [m.content for m in model.calls[0][1:-1]] == ["hi", "hello"]
    stored = await store.get_messages("s1")
    assert str(stored[-1].content).startswith(COMPACTION_HEADER) and "SUM" in str(
        stored[-1].content
    )
    spent = [e for e in bus.published if e.type == EventType.MODEL_USAGE]
    assert spent and spent[0].session_id == "s1"
    assert await runtime.compact("s1") == 0  # nothing new since the summary


async def test_files_outside_the_workspace_are_never_attached(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = InMemorySessionStore()
    calls = [{"name": "read_file", "args": {"path": "../secret.txt"}, "id": "r1"}]
    await store.append_messages(
        "s1", [HumanMessage(content="hi"), AIMessage(content="", tool_calls=calls)]
    )
    await store.append_messages("s1", [ToolMessage(content="x", tool_call_id="r1")])
    runtime = _compacting_runtime(FakeChatModel([ai("SUM")]), store, workspace_root=workspace)

    await runtime.compact("s1")

    assert "TOP SECRET" not in str((await store.get_messages("s1"))[-1].content)


async def test_concurrent_runs_calibrate_against_their_own_estimate() -> None:
    """Two interleaved runs on one runtime: each run's reported input tokens are compared
    with that run's own estimate, not with whichever run built its input last."""
    long = "long " * 500
    estimates: dict[str, int] = {}
    calibrations: list[tuple[int, int]] = []

    class SpyBuilder(ContextBuilder):
        def build(self, **kwargs: Any) -> Any:
            ctx = super().build(**kwargs)
            estimates[str(kwargs["pinned"].content)] = ctx.raw_estimate
            return ctx

        def calibrate(self, raw_estimate: int, actual_tokens: int, overhead: int = 0) -> None:
            calibrations.append((raw_estimate, actual_tokens))

    both_called = asyncio.Event()
    calls = 0

    class BarrierModel:
        """Holds the first call until the second run has built its input too."""

        def bind_tools(self, tools: Any) -> BarrierModel:
            return self

        async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
            nonlocal calls
            calls += 1
            if calls == 2:
                both_called.set()
            await both_called.wait()
            tokens = 111 if messages[-1].content == "short" else 999
            return AIMessage(
                content="ok",
                usage_metadata={"input_tokens": tokens, "output_tokens": 1, "total_tokens": 0},
            )

    runtime = _runtime(BarrierModel(), InMemorySessionStore(), context_builder=SpyBuilder())
    await asyncio.gather(
        runtime.run(session_id="a", user_id="u1", message="short"),
        runtime.run(session_id="b", user_id="u1", message=long),
    )

    assert estimates["short"] != estimates[long]
    assert sorted(calibrations) == sorted([(estimates["short"], 111), (estimates[long], 999)])
