"""Unit tests for the explicit model/tool loop.

Driven entirely by deterministic doubles: `FakeChatModel` for the model, an inline
duck-typed echo tool, and real `ToolRegistry`/`ToolExecutor`. No network, no real LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agent_stack.core.agent import Agent
from agent_stack.core.errors import StepLimitExceeded
from agent_stack.core.limits import AgentLimits
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FailingTool, FakeChatModel, ai, call


class EchoTool:
    """A tiny duck-typed tool; the registry/executor only need name/description/ainvoke."""

    name = "echo"
    description = "Echo the arguments back."

    async def ainvoke(self, args: dict[str, Any]) -> str:
        return f"echo: {args}"


def _build_runtime(
    model: Any,
    tools: Sequence[Any],
    store: InMemorySessionStore,
    *,
    limits: AgentLimits | None = None,
) -> AgentRuntime:
    registry = ToolRegistry(tools)  # type: ignore[arg-type]
    executor = ToolExecutor(registry)
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(tools),  # type: ignore[arg-type]
        system_prompt="You are a test assistant.",
        limits=limits or AgentLimits(),
    )
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=executor,
        session_store=store,
    )


def _contents(messages: Sequence[BaseMessage]) -> list[Any]:
    return [message.content for message in messages]


async def test_returns_direct_model_response() -> None:
    store = InMemorySessionStore()
    runtime = _build_runtime(FakeChatModel([ai("hi")]), [], store)

    response = await runtime.run(session_id="s1", user_id="u1", message="hello")

    assert response.content == "hi"
    stored = await store.get_messages("s1")
    assert len(stored) == 2
    assert isinstance(stored[0], HumanMessage)
    assert stored[0].content == "hello"
    assert isinstance(stored[1], AIMessage)
    assert stored[1].content == "hi"


async def test_executes_tool_round_trip() -> None:
    store = InMemorySessionStore()
    model = FakeChatModel([ai(tool_calls=[call("echo", {"x": 1})]), ai("done")])
    runtime = _build_runtime(model, [EchoTool()], store)

    response = await runtime.run(session_id="s1", user_id="u1", message="go")

    assert response.content == "done"
    stored = await store.get_messages("s1")
    assert len(stored) == 4

    assert isinstance(stored[0], HumanMessage)
    assert isinstance(stored[1], AIMessage)
    assert stored[1].tool_calls[0]["name"] == "echo"
    assert isinstance(stored[2], ToolMessage)
    assert stored[2].tool_call_id == "call-1"
    assert isinstance(stored[3], AIMessage)
    assert stored[3].content == "done"


async def test_persists_without_duplication() -> None:
    store = InMemorySessionStore()

    first = _build_runtime(FakeChatModel([ai("a")]), [], store)
    await first.run(session_id="s1", user_id="u1", message="one")

    second = _build_runtime(FakeChatModel([ai("b")]), [], store)
    await second.run(session_id="s1", user_id="u1", message="two")

    stored = await store.get_messages("s1")
    assert len(stored) == 4
    assert _contents(stored) == ["one", "a", "two", "b"]


async def test_tool_error_becomes_tool_message() -> None:
    store = InMemorySessionStore()
    model = FakeChatModel([ai(tool_calls=[call("boom", {})]), ai("recovered")])
    runtime = _build_runtime(model, [FailingTool()], store)

    response = await runtime.run(session_id="s1", user_id="u1", message="go")

    assert response.content == "recovered"
    stored = await store.get_messages("s1")
    tool_messages = [message for message in stored if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].status == "error"


async def test_stops_after_max_steps() -> None:
    store = InMemorySessionStore()
    model = FakeChatModel(
        [
            ai(tool_calls=[call("echo", {"x": 1})]),
            ai(tool_calls=[call("echo", {"x": 2})]),
            ai(tool_calls=[call("echo", {"x": 3})]),
        ]
    )
    runtime = _build_runtime(model, [EchoTool()], store, limits=AgentLimits(max_steps=1))

    with pytest.raises(StepLimitExceeded):
        await runtime.run(session_id="s1", user_id="u1", message="go")


async def test_session_created_on_first_run() -> None:
    store = InMemorySessionStore()
    runtime = _build_runtime(FakeChatModel([ai("hi")]), [], store)

    await runtime.run(session_id="fresh", user_id="u9", message="hello")

    session = await store.get("fresh")
    assert session is not None
    assert session.owner_id == "u9"
    assert session.agent_name == "assistant"
