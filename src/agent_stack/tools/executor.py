"""Turn model tool calls into ``ToolMessage``s, concurrently and safely.

A *tool call* is shaped like a LangChain ``AIMessage.tool_calls`` entry::

    {"name": str, "args": dict, "id": str, "type": "tool_call"}

Every failure — unknown tool name or a raising tool — is captured and returned as
an error ``ToolMessage`` rather than propagated, so one bad call never crashes the
model/tool loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import ToolMessage

from agent_stack.tools.registry import ToolRegistry

__all__ = ["ToolExecutor"]


class ToolExecutor:
    """Executes tool calls against a :class:`ToolRegistry`."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute_one(self, tool_call: Mapping[str, Any]) -> ToolMessage:
        """Execute a single tool call, returning a ``ToolMessage`` either way."""
        tool_call_id: str = tool_call["id"]

        try:
            tool = self.registry.get(tool_call["name"])
            # Invoke with the *arguments*, never the whole tool_call dict.
            result = await tool.ainvoke(tool_call["args"])
        except Exception as exc:
            return ToolMessage(
                content=f"Error: {exc}",
                tool_call_id=tool_call_id,
                status="error",
            )

        return ToolMessage(content=str(result), tool_call_id=tool_call_id)

    async def execute_many(self, tool_calls: Sequence[Mapping[str, Any]]) -> list[ToolMessage]:
        """Execute every call concurrently, preserving the input order."""
        if not tool_calls:
            return []

        results = await asyncio.gather(*(self.execute_one(tool_call) for tool_call in tool_calls))
        return list(results)
