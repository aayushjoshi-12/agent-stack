from langchain.chat_models import init_chat_model

from agent_stack.core.agent import AgentConfig
from agent_stack.core.runtime import AgentRuntime
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry


class AgentFactory:
    def __init__(self, event_bus, session_store, checkpoint_store):
        self.event_bus = event_bus
        self.session_store = session_store
        self.checkpoint_store = checkpoint_store

    def create(self, agent_config: AgentConfig) -> AgentRuntime:
        model = init_chat_model(agent_config.model)
        model_with_tools = model.bind_tools(agent_config.tools)
        tool_registry = ToolRegistry(agent_config.tools)
        tool_executor = ToolExecutor(tool_registry)
        return AgentRuntime(
            agent_config, model_with_tools, tool_executor, session_store=self.session_store
        )
