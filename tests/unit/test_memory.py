"""Acceptance tests for Phase 16d — file-based memory (``MEMORY.md``).

Level 1 (the index) is resident in the system prompt; level 2 (a topic body) is read on
demand and must never leak into the resident block. The wiring tests drive the real
``AgentRuntime`` with a deterministic ``FakeChatModel`` and assert on the exact message
list the model received. No network, no real LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import SystemMessage

from agent_stack.context.composer import SystemPromptComposer
from agent_stack.context.instructions import InstructionsLoader
from agent_stack.core.agent import Agent
from agent_stack.core.context import ContextBuilder
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.memory import MAX_INDEX_BYTES, MemoryFilesError, MemoryIndex, memory_tools
from agent_stack.skills import SkillLoader, SkillRegistry
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call


def _workspace(tmp_path: Path) -> Path:
    """A workspace with a memory index line and a topic whose body is the real answer."""
    workspace = tmp_path / "ws"
    memory = workspace / ".agent" / "memory"
    (memory / "topics").mkdir(parents=True)
    (memory / "MEMORY.md").write_text("Deployment — ships on Fridays.\n", encoding="utf-8")
    (memory / "topics" / "deployment.md").write_text("SECRET-BODY-99", encoding="utf-8")
    return workspace


def _composer(workspace: Path) -> SystemPromptComposer:
    return SystemPromptComposer(
        instructions=InstructionsLoader(),
        skills=SkillRegistry(SkillLoader([workspace / ".agent" / "skills"])),
        memory=MemoryIndex(workspace),
        workspace_root=workspace,
    )


def _runtime(
    model: Any,
    store: InMemorySessionStore,
    *,
    composer: SystemPromptComposer,
    tools: tuple[Any, ...] = (),
) -> AgentRuntime:
    agent = Agent(name="assistant", model="test:model", tools=tools, system_prompt="BASE PROMPT")
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(tools)),
        session_store=store,
        context_builder=ContextBuilder(),
        system_parts=composer,
    )


async def test_memory_index_loaded_capped(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    model = FakeChatModel([ai("ok")])
    runtime = _runtime(model, InMemorySessionStore(), composer=_composer(workspace))
    await runtime.run(session_id="s1", user_id="u1", message="hello")

    system = model.calls[0][0]
    assert isinstance(system, SystemMessage)
    # Level 1 is resident: the index line, as name + summary.
    assert "Deployment" in system.content
    assert "ships on Fridays" in system.content
    # Level 2 (the topic body) is not — it must be fetched via read_memory_topic.
    assert "SECRET-BODY-99" not in system.content

    # The resident index is capped like the instruction/skill budgets.
    (workspace / ".agent" / "memory" / "MEMORY.md").write_text(
        "x" * (MAX_INDEX_BYTES + 1000), encoding="utf-8"
    )
    capped = MemoryIndex(workspace).index()
    assert len(capped.encode("utf-8")) <= MAX_INDEX_BYTES


async def test_read_topic_on_demand_scoped_to_memory_dir(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    memory = MemoryIndex(workspace)

    # On demand and in scope, the body is returned.
    assert memory.read_topic("Deployment") == "SECRET-BODY-99"

    # Path traversal is rejected: directly and through the tool (as an error string).
    tools = {tool.name: tool for tool in memory_tools(memory)}
    for escaping in ("../MEMORY.md", "../../etc/passwd", "/etc/passwd", "..", "a/b"):
        with pytest.raises(MemoryFilesError):
            memory.read_topic(escaping)
        result = await tools["read_memory_topic"].ainvoke({"name": escaping})
        assert result.startswith("Error:")

    # On demand through the real loop: the model fetches the body the index did not carry.
    model = FakeChatModel(
        [ai(tool_calls=[call("read_memory_topic", {"name": "Deployment"})]), ai("done")]
    )
    runtime = _runtime(
        model, InMemorySessionStore(), composer=_composer(workspace), tools=memory_tools(memory)
    )
    await runtime.run(session_id="s1", user_id="u1", message="when does deployment ship?")

    second_call = model.calls[1]
    assert any("SECRET-BODY-99" in str(message.content) for message in second_call)


async def test_write_topic_updates_index_entry(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    memory = MemoryIndex(workspace)
    index_file = workspace / ".agent" / "memory" / "MEMORY.md"

    # Updating an existing topic rewrites its index line in place (no duplicate).
    memory.write_topic("Deployment", "# Deployment\n\nNow ships on Tuesdays.")
    text = index_file.read_text(encoding="utf-8")
    assert text.count("Deployment") == 1
    assert "Now ships on Tuesdays." in text
    assert "ships on Fridays" not in text

    # A new topic appends a line, and the topic file carries `updated` frontmatter.
    memory.write_topic("Rollback", "Revert with `make rollback`.")
    assert "Rollback" in index_file.read_text(encoding="utf-8")
    topic = (workspace / ".agent" / "memory" / "topics" / "deployment.md").read_text(
        encoding="utf-8"
    )
    assert topic.startswith("---\nupdated: ")
    assert memory.read_topic("Deployment") == "# Deployment\n\nNow ships on Tuesdays."
