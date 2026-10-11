"""Acceptance tests for Phase 16e — context wiring reaches the model.

These drive the real ``AgentRuntime`` with a deterministic ``FakeChatModel`` and assert
on the exact message list the model received: resident system parts (instructions, skill
metadata) and the bounded history window. No network, no real LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from agent_stack.context.composer import SystemPromptComposer
from agent_stack.context.instructions import InstructionsLoader
from agent_stack.core.agent import Agent
from agent_stack.core.context import ContextBuilder
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.memory import MemoryIndex
from agent_stack.skills import SkillLoader, SkillRegistry
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai


class FakeSummarizer:
    """Records the tail it summarized and returns a fixed summary."""

    def __init__(self) -> None:
        self.summarized: list[Sequence[BaseMessage]] = []

    async def summarize(self, messages: Sequence[BaseMessage]) -> str:
        self.summarized.append(messages)
        return "SUMMARY"


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


async def test_history_summary_reaches_model_prompt() -> None:
    store = InMemorySessionStore()
    history = [HumanMessage(content=f"h{i}") for i in range(10)]
    await store.append_messages("s1", history)

    summarizer = FakeSummarizer()
    model = FakeChatModel([ai("ok")])
    runtime = _runtime(
        model, store, context_builder=ContextBuilder(recent_window=4, summarizer=summarizer)
    )

    await runtime.run(session_id="s1", user_id="u1", message="new")

    # The old tail was summarized, and the summary survives re-windowing as a system part.
    assert summarizer.summarized == [history[:-4]]
    system = model.calls[0][0]
    assert isinstance(system, SystemMessage)
    assert "SUMMARY" in system.content
    assert len(model.calls[0][1:]) == 4
