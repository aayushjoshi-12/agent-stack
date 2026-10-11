"""Name → ``BaseTool`` lookup with collision and unknown-name rejection."""

from __future__ import annotations

from langchain_core.tools import BaseTool

__all__ = ["ToolRegistry"]


class ToolRegistry:
    """An immutable-by-convention collection of tools keyed by name."""

    def __init__(self, tools: tuple[BaseTool, ...]) -> None:
        self._tools: dict[str, BaseTool] = {}

        for tool in tools:
            if tool.name in self._tools:
                raise ValueError(f"Duplicate tool name: {tool.name}")

            self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"Unknown tool: {name}") from None

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())
