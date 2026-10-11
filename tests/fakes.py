"""Deterministic test doubles.

The runtime must be testable without real LLM calls. `FakeChatModel` is a minimal
duck-typed chat model that returns scripted `AIMessage`s, so the model/tool loop can be
exercised end-to-end in milliseconds.

Tests are not type-checked (mypy runs on `src` only), so this file favours clarity over
strict typing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage


def ai(
    content: str = "",
    *,
    tool_calls: Sequence[dict[str, Any]] = (),
    id: str | None = None,
    reasoning: str | None = None,
) -> AIMessage:
    """Build an AIMessage that may carry tool calls and/or reasoning."""
    kwargs = {"reasoning_content": reasoning} if reasoning is not None else {}
    return AIMessage(content=content, tool_calls=list(tool_calls), id=id, additional_kwargs=kwargs)


def call(name: str, args: dict[str, Any] | None = None, *, id: str = "call-1") -> dict[str, Any]:
    """Build a single tool call dict, shaped like LangChain's `AIMessage.tool_calls`."""
    return {"name": name, "args": args or {}, "id": id, "type": "tool_call"}


class FakeChatModel:
    """A scripted chat model.

    Not a `BaseChatModel` subclass — it implements only the surface the runtime uses
    (`bind_tools`, `ainvoke`). Pass an instance wherever a chat model is expected.
    """

    def __init__(self, responses: Sequence[AIMessage]) -> None:
        self._responses = list(responses)
        self.bound_tools: list[Any] = []
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Sequence[Any]) -> FakeChatModel:
        self.bound_tools = list(tools)
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError(
                "FakeChatModel exhausted: the runtime asked for more responses than were "
                "scripted (possible runaway loop)."
            )
        return self._responses.pop(0)


class SleepTool:
    """A tool that sleeps for a fixed duration; used to prove parallel execution."""

    def __init__(self, name: str, seconds: float) -> None:
        self.name = name
        self.description = f"Sleep for {seconds}s."
        self._seconds = seconds

    async def ainvoke(self, args: dict[str, Any]) -> str:
        import asyncio

        await asyncio.sleep(self._seconds)
        return f"{self.name} slept {self._seconds}s"


class FailingTool:
    """A tool that always raises; used to prove tool errors become ToolMessages."""

    def __init__(self, name: str = "boom", message: str = "kaboom") -> None:
        self.name = name
        self.description = "Always fails."
        self._message = message

    async def ainvoke(self, args: dict[str, Any]) -> str:
        raise RuntimeError(self._message)
