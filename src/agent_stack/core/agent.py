from dataclasses import dataclass

from langchain_core.tools import BaseTool


@dataclass(frozen=True)
class AgentConfig:
    """AgentConfig definition that is used to create an agent runtime."""

    name: str
    model: str
    tools: tuple[BaseTool, ...]
    system_prompt: str
    subagents: tuple["AgentConfig", ...] = ()
