from __future__ import annotations

from typing import cast

from langchain_core.language_models import BaseChatModel

from agent_stack.artifacts.base import ArtifactStore
from agent_stack.core.agent import Agent
from agent_stack.core.approval import ApprovalBroker
from agent_stack.core.context import ContextBuilder, SystemPartsProvider
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
    tools) and a tool executor around them. The optional ``context_builder`` and
    ``system_parts`` are threaded through to every runtime it builds, so subagent runtimes
    see the same instructions/skills context as their parent.
    """

    def __init__(
        self,
        *,
        session_store: SessionStore,
        event_bus: EventBus | None = None,
        session_lock: SessionLockManager | None = None,
        context_builder: ContextBuilder | None = None,
        system_parts: SystemPartsProvider | None = None,
        approvals: ApprovalBroker | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._session_store = session_store
        self._event_bus = event_bus
        self._session_lock = session_lock
        self._context_builder = context_builder
        self._system_parts = system_parts
        self._approvals = approvals
        self._artifacts = artifacts

    def create(self, agent: Agent) -> AgentRuntime:
        model = build_model(agent.model)
        model_with_tools = cast(BaseChatModel, model.bind_tools(list(agent.tools)))
        registry = ToolRegistry(agent.tools)
        executor = ToolExecutor(
            registry,
            timeout_seconds=agent.limits.tool_timeout_seconds,
            artifacts=self._artifacts,
        )
        return AgentRuntime(
            agent=agent,
            model=model_with_tools,
            tool_executor=executor,
            session_store=self._session_store,
            event_bus=self._event_bus,
            session_lock=self._session_lock,
            context_builder=self._context_builder,
            system_parts=self._system_parts,
            approvals=self._approvals,
        )
