"""``remember`` / ``recall`` / ``forget``: the model's explicit, persistent fact store (P21).

Facts live in the *owner's* namespace (the run's user), so they carry across sessions and are
never visible to another user. They are short keyed facts; longer project notes belong in the
file-based memory topics instead.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from agent_stack.core.run_context import current_run_context
from agent_stack.memory.store import MemoryStore

__all__ = ["persistent_memory_tools"]

_NO_RUN = "Error: persistent memory is only available inside a run."


def persistent_memory_tools(store: MemoryStore) -> tuple[BaseTool, ...]:
    """Build the three persistent-memory tools bound to ``store``."""

    def namespace() -> str | None:
        context = current_run_context()
        return None if context is None else f"user:{context.user_id}"

    @tool
    async def remember(key: str, value: str) -> str:
        """Save a short durable fact about the user or their preferences for future sessions.

        ``key`` is a brief label (e.g. ``preferred_language``); ``value`` is the fact.
        Saving under an existing key replaces its value. Only store what the user would want
        remembered; never store secrets.
        """
        ns = namespace()
        if ns is None:
            return _NO_RUN
        item = await store.remember(ns, key.strip(), value.strip())
        return f"Remembered {item.key!r}."

    @tool
    async def recall(query: str = "") -> str:
        """Look up remembered facts by keyword. An empty query lists everything remembered.

        Every word of ``query`` must appear in a fact's key or value.
        """
        ns = namespace()
        if ns is None:
            return _NO_RUN
        items = await store.recall(ns, query)
        if not items:
            return "Nothing remembered matches." if query.strip() else "Nothing remembered yet."
        return "\n".join(f"{item.key}: {item.value}" for item in items)

    @tool
    async def forget(key: str) -> str:
        """Delete a remembered fact by its exact key."""
        ns = namespace()
        if ns is None:
            return _NO_RUN
        removed = await store.forget(ns, key.strip())
        return (
            f"Forgot {key.strip()!r}." if removed else f"No remembered fact named {key.strip()!r}."
        )

    return (remember, recall, forget)
