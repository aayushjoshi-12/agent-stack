"""The agent definition — a frozen description of *what* an agent is.

An :class:`Agent` owns configuration only. It never holds per-run state (`AgentState`
does that) or infrastructure (`AgentRuntime` does that).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.tools import BaseTool

from agent_stack.core.limits import AgentLimits


@dataclass(frozen=True)
class SubAgentSpec:
    """A named delegation target: a description plus the child :class:`Agent`."""

    name: str
    description: str
    agent: Agent


@dataclass(frozen=True)
class Agent:
    """A frozen agent definition.

    `tools` are the tools this agent may call; `subagents` are delegation targets.
    """

    name: str
    model: str
    tools: tuple[BaseTool, ...] = ()
    subagents: tuple[SubAgentSpec, ...] = ()
    system_prompt: str = ""
    limits: AgentLimits = field(default_factory=AgentLimits)
