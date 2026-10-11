from langchain.chat_models import BaseChatModel
from langchain.messages import HumanMessage, SystemMessage

from agent_stack.core.agent import AgentConfig
from agent_stack.core.session import SessionStore
from agent_stack.tools.executor import ToolExecutor


class AgentRuntime:
    def __init__(
        self,
        agent_config: AgentConfig,
        model_with_tools: BaseChatModel,
        tool_executor: ToolExecutor,
        session_store: SessionStore,
    ):
        self.agent_config = agent_config
        self.model_with_tools = model_with_tools
        self.tool_executor = tool_executor
        self.session_store = session_store

    async def run(self, message: str, session_id):
        messages = self.session_store.get_messages(session_id)
        messages.append(HumanMessage(content=message))

        while True:
            response = await self.model_with_tools.ainvoke(
                [SystemMessage(self.agent_config.system_prompt), *messages]
            )
            messages.append(response)

            if not response.tool_calls:
                break

            tool_message = await self.tool_executor.execute_many(response.tool_calls)
            messages.extend(tool_message)

        self.session_store.add_messages(session_id=session_id, messages=messages)
        return response
