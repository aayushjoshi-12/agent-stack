"""Memory tools: ``read_memory_topic`` (level 2) and ``write_memory_topic`` (level 3).

These implement progressive disclosure for file-based memory (P16d), exactly as
``skills/tools.py`` does for skills: the model sees only the ``MEMORY.md`` index in its
system prompt, reads topic bodies on demand, and writes topics (keeping their index line
in sync) on demand. Errors are returned as short strings, never raised.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from agent_stack.memory.files import MemoryFilesError, MemoryIndex

__all__ = ["memory_tools"]


def memory_tools(index: MemoryIndex) -> tuple[BaseTool, ...]:
    """Build the progressive-disclosure memory tool set bound to ``index``."""

    @tool
    async def read_memory_topic(name: str) -> str:
        """Read the full notes of a named memory topic.

        Memory topics are listed in the system prompt by name and summary only; call this
        to read a topic's full content before relying on it.
        """
        try:
            return index.read_topic(name)
        except MemoryFilesError as exc:
            return f"Error: {exc}"

    @tool
    async def write_memory_topic(name: str, content: str) -> str:
        """Create or update a memory topic and its index line.

        Use this to remember a durable fact, decision, or convention. ``name`` is a short
        topic label; ``content`` is the full markdown body to store.
        """
        try:
            index.write_topic(name, content)
        except MemoryFilesError as exc:
            return f"Error: {exc}"
        return f"Saved memory topic {name!r}."

    return (read_memory_topic, write_memory_topic)
