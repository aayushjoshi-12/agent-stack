from __future__ import annotations

from typing import cast

from langchain_core.language_models import BaseChatModel

from agent_stack.core.agent import Agent
from agent_stack.core.event_bus import EventBus
from agent_stack.core.locks import SessionLockManager
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import SessionStore
from agent_stack.models.factory import build_model
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry


class AgentFactory:
    """Builds a runnable ``AgentRuntime`` from an ``Agent`` definition.

    Shared stores are injected once; each ``create`` wires a model (bound to the agent's
    tools) and a tool executor around them.
    """

    def __init__(
        self,
        *,
        session_store: SessionStore,
        event_bus: EventBus | None = None,
        session_lock: SessionLockManager | None = None,
    ) -> None:
        self._session_store = session_store
        self._event_bus = event_bus
        self._session_lock = session_lock

    def create(self, agent: Agent) -> AgentRuntime:
        model = build_model(agent.model)
        model_with_tools = cast(BaseChatModel, model.bind_tools(list(agent.tools)))
        registry = ToolRegistry(agent.tools)
        executor = ToolExecutor(registry)
        return AgentRuntime(
            agent=agent,
            model=model_with_tools,
            tool_executor=executor,
            session_store=self._session_store,
            event_bus=self._event_bus,
            session_lock=self._session_lock,
        )
