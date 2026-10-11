"""Built-in tools (filesystem, terminal, memory, todos) live here.

Populated in later phases; this package exists so the tool layer is import-safe.
"""

from __future__ import annotations

from agent_stack.tools.builtins.filesystem import filesystem_tools
from agent_stack.tools.builtins.terminal import CommandResult, run_command, run_command_tool

__all__ = [
    "CommandResult",
    "filesystem_tools",
    "run_command",
    "run_command_tool",
]
