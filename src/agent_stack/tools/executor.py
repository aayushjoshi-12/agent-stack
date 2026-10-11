"""Turn model tool calls into ``ToolExecutionResult``s, concurrently and safely.

A *tool call* is shaped like a LangChain ``AIMessage.tool_calls`` entry::

    {"name": str, "args": dict, "id": str, "type": "tool_call"}

Every failure — unknown tool name or a raising tool — is captured and returned as
an error result rather than propagated, so one bad call never crashes the
model/tool loop.

A :class:`ToolExecutionResult` is the runtime-level outcome of one call; its
``message`` (a :class:`~langchain_core.messages.ToolMessage`) is the model-facing
projection the runtime feeds back into the conversation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage

from agent_stack.tools.registry import ToolRegistry

__all__ = ["ToolExecutionResult", "ToolExecutor"]


@dataclass(frozen=True)
class ToolExecutionResult:
    """Runtime-level outcome of one tool call.

    Distinct from the model-facing :class:`ToolMessage`, which is its ``message``
    projection: the executor produces results, the runtime feeds ``.message`` to the
    model and records the result for idempotency.
    """

    tool_call_id: str
    tool_name: str
    message: ToolMessage
    success: bool
    error: str | None = None


class ToolExecutor:
    """Executes tool calls against a :class:`ToolRegistry`."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute_one(self, tool_call: Mapping[str, Any]) -> ToolExecutionResult:
        """Execute a single tool call, returning a result either way."""
        tool_call_id: str = tool_call["id"]
        tool_name = str(tool_call["name"])

        try:
            tool = self.registry.get(tool_name)
            # Invoke with the *arguments*, never the whole tool_call dict.
            result = await tool.ainvoke(tool_call["args"])
        except Exception as exc:
            return ToolExecutionResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                message=ToolMessage(
                    content=f"Error: {exc}",
                    tool_call_id=tool_call_id,
                    status="error",
                ),
                success=False,
                error=str(exc),
            )

        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            message=ToolMessage(content=str(result), tool_call_id=tool_call_id),
            success=True,
        )

    async def execute_many(
        self, tool_calls: Sequence[Mapping[str, Any]]
    ) -> list[ToolExecutionResult]:
        """Execute every call concurrently, preserving the input order."""
        if not tool_calls:
            return []

        results = await asyncio.gather(*(self.execute_one(tool_call) for tool_call in tool_calls))
        return list(results)
