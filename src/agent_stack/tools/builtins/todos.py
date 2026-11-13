"""``todo_list`` / ``todo_add`` / ``todo_complete``: a small explicit task list (P22).

The list is the session's, so it is still there when the run is resumed or the user sends
another message. It is a plain checklist the model keeps for itself; there is no planner.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from agent_stack.core.run_context import current_run_context
from agent_stack.core.state import TodoItem, TodoStatus
from agent_stack.persistence.todos import SqliteTodoStore

__all__ = ["todo_tools"]

_NO_RUN = "Error: todos are only available inside a run."


def _line(item: TodoItem) -> str:
    mark = "x" if item.status is TodoStatus.DONE else " "
    return f"[{mark}] {item.id}. {item.text}"


def todo_tools(store: SqliteTodoStore) -> tuple[BaseTool, ...]:
    """Build the three todo tools bound to ``store``."""

    def session() -> str | None:
        context = current_run_context()
        return None if context is None else context.session_id

    @tool
    async def todo_list() -> str:
        """Show the current task list, each item with its number and whether it is done."""
        session_id = session()
        if session_id is None:
            return _NO_RUN
        items = await store.list_items(session_id)
        return "\n".join(_line(i) for i in items) if items else "No todos."

    @tool
    async def todo_add(text: str) -> str:
        """Add a step to the task list. Use it to plan multi-step work before starting."""
        session_id = session()
        if session_id is None:
            return _NO_RUN
        item = await store.add(session_id, text.strip())
        return f"Added todo {item.id}: {item.text}"

    @tool
    async def todo_complete(id: int) -> str:
        """Mark the todo with this number as done."""
        session_id = session()
        if session_id is None:
            return _NO_RUN
        if not await store.complete(session_id, id):
            return f"No todo {id}."
        return f"Completed todo {id}."

    return (todo_list, todo_add, todo_complete)
