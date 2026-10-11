"""Unit tests for :class:`agent_stack.tools.registry.ToolRegistry`."""

from __future__ import annotations

import pytest

from agent_stack.tools.registry import ToolRegistry


class _EchoTool:
    """Minimal duck-typed tool: only ``name``/``description``/``ainvoke`` are used."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"Echo for {name}."

    async def ainvoke(self, args):
        return args


def test_get_returns_tool() -> None:
    tool = _EchoTool("echo")
    registry = ToolRegistry((tool,))

    assert registry.get("echo") is tool


def test_all_returns_tools() -> None:
    first = _EchoTool("a")
    second = _EchoTool("b")
    registry = ToolRegistry((first, second))

    assert registry.all() == [first, second]


def test_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="Duplicate tool name: dup"):
        ToolRegistry((_EchoTool("dup"), _EchoTool("dup")))


def test_raises_on_unknown_tool() -> None:
    registry = ToolRegistry((_EchoTool("known"),))

    with pytest.raises(KeyError, match="Unknown tool: missing"):
        registry.get("missing")
