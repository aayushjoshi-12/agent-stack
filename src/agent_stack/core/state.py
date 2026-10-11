"""Mutable execution state for a single agent run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage


@dataclass
class AgentState:
    """The current mutable state of a run.

    Deliberately small: `messages` carry the conversation, the counters feed limit
    enforcement, and `metadata` is scratch space for runtime bookkeeping.
    """

    messages: list[BaseMessage] = field(default_factory=list)
    step_count: int = 0
    tool_call_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
