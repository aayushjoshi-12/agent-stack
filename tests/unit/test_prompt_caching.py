"""Prompt-cache breakpoints (P37): placed where the provider needs them, nowhere else."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from agent_stack.models.caching import (
    CACHE_CONTROL,
    add_cache_breakpoints,
    needs_breakpoints,
    with_prompt_cache,
)
from tests.fakes import FakeChatModel, call


def _conversation() -> list[BaseMessage]:
    return [
        SystemMessage(content="You are helpful."),
        HumanMessage(content="read a.txt"),
        AIMessage(content="", tool_calls=[call("read_file", {"path": "a.txt"})]),
        ToolMessage(content="hello", tool_call_id="c1"),
    ]


def _marks(messages: list[BaseMessage]) -> list[int]:
    return [
        i
        for i, m in enumerate(messages)
        if isinstance(m.content, list)
        and any(isinstance(b, dict) and "cache_control" in b for b in m.content)
    ]


def test_system_and_last_message_get_a_breakpoint_and_nothing_else_changes() -> None:
    before = _conversation()
    snapshot = [m.model_copy(deep=True) for m in before]

    out = add_cache_breakpoints(before)

    assert _marks(out) == [0, 3]
    assert out[0].content == [
        {"type": "text", "text": "You are helpful.", "cache_control": CACHE_CONTROL}
    ]
    assert out[3].content == [{"type": "text", "text": "hello", "cache_control": CACHE_CONTROL}]
    assert isinstance(out[3], ToolMessage) and out[3].tool_call_id == "c1"
    assert out[1] is before[1] and out[2] is before[2]  # untouched messages are passed through
    assert before == snapshot  # the stored messages are never mutated


def test_a_message_with_no_text_passes_the_breakpoint_to_the_one_before() -> None:
    messages = _conversation()[:3]  # ends on a tool-call-only assistant turn
    assert _marks(add_cache_breakpoints(messages)) == [0, 1]


def test_list_content_marks_only_the_last_text_block() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:x"}}
    first = {"type": "text", "text": "one"}
    human = HumanMessage(content=[first, {"type": "text", "text": "two"}, image])

    out = add_cache_breakpoints([SystemMessage(content="s"), human])

    assert out[1].content == [
        first,
        {"type": "text", "text": "two", "cache_control": CACHE_CONTROL},
        image,
    ]
    assert "cache_control" not in human.content[1]  # type: ignore[operator]


def test_never_more_than_two_breakpoints_and_no_system_is_fine() -> None:
    long = [SystemMessage(content="s")] + [HumanMessage(content=f"m{i}") for i in range(30)]
    assert len(_marks(add_cache_breakpoints(long))) == 2
    assert _marks(add_cache_breakpoints([HumanMessage(content="only")])) == [0]
    assert add_cache_breakpoints([]) == []


@pytest.mark.parametrize(
    ("model", "explicit"),
    [
        ("anthropic:claude-haiku-4-5", True),
        ("openrouter:anthropic/claude-haiku-4.5", True),
        ("openrouter:qwen/qwen3-max", True),
        ("openrouter:deepseek/deepseek-v4-pro", False),
        ("openrouter:openai/gpt-5-mini", False),
        ("openrouter:google/gemini-2.5-flash", False),
        ("openai:gpt-5", False),
    ],
)
def test_only_providers_that_cache_at_explicit_marks_get_breakpoints(
    model: str, explicit: bool
) -> None:
    assert needs_breakpoints(model) is explicit


async def test_wrapped_model_sees_breakpoints_and_others_are_returned_unchanged() -> None:
    fake = FakeChatModel([AIMessage(content="ok")])
    inner: Any = RunnableLambda(fake.ainvoke)

    assert with_prompt_cache(inner, "openrouter:deepseek/deepseek-v4-pro") is inner

    messages = _conversation()
    await with_prompt_cache(inner, "openrouter:anthropic/claude-haiku-4.5").ainvoke(messages)

    assert _marks(fake.calls[0]) == [0, 3]
    assert _marks(messages) == []
