"""Execution limits that bound an agent run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentLimits:
    """Immutable limits that guarantee a run terminates.

    These are definitions only; enforcement lives with the runtime.
    """

    max_steps: int = 50
    max_tool_calls: int = 100
    max_runtime_seconds: float = 1800.0
    max_concurrent_tools: int = 8
    max_subagent_depth: int = 2
    max_subagents: int = 8
