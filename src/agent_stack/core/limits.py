"""Execution limits that bound an agent run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentLimits:
    """Immutable limits that guarantee a run terminates.

    Definitions only: the runtime enforces ``max_steps``, ``max_tool_calls`` and
    ``max_runtime_seconds``; the executor enforces ``tool_timeout_seconds``.
    """

    max_steps: int = 50
    max_tool_calls: int = 100
    max_runtime_seconds: float = 1800.0
    #: Longest a single tool call may run before it becomes an error ToolMessage; None: no cap.
    tool_timeout_seconds: float | None = 300.0
    max_concurrent_tools: int = 8
    max_subagent_depth: int = 2
    max_subagents: int = 8
