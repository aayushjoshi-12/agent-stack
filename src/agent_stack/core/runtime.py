from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent_stack.core.agent import Agent
from agent_stack.core.errors import (
    AgentStackError,
    StepLimitExceeded,
    ToolCallLimitExceeded,
)
from agent_stack.core.session import Session, SessionStore
from agent_stack.tools.executor import ToolExecutor


class AgentRuntime:
    """The explicit model/tool loop.

    The loop is deliberately boring: build a context, ask the model, execute any tool
    calls, repeat until the model returns a final answer. Persistence, limits, events and
    subagents surround this loop; the loop itself does not grow branches for them.

    The ``model`` is expected to be already bound to the agent's tools by ``AgentFactory``.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        model: BaseChatModel,
        tool_executor: ToolExecutor,
        session_store: SessionStore,
    ) -> None:
        self._agent = agent
        self._model = model
        self._tool_executor = tool_executor
        self._session_store = session_store

    async def run(self, *, session_id: str, user_id: str, message: str) -> AIMessage:
        session = await self._session_store.get(session_id)
        if session is None:
            session = Session(id=session_id, owner_id=user_id, agent_name=self._agent.name)
            await self._session_store.save(session)

        history = await self._session_store.get_messages(session_id)
        new_messages: list[BaseMessage] = [HumanMessage(content=message)]

        limits = self._agent.limits
        step_count = 0
        tool_call_count = 0

        while True:
            response = await self._model.ainvoke(
                [SystemMessage(content=self._agent.system_prompt), *history, *new_messages]
            )
            if not isinstance(response, AIMessage):
                raise AgentStackError(
                    f"Model returned {type(response).__name__}, expected AIMessage"
                )
            new_messages.append(response)
            step_count += 1

            if not response.tool_calls:
                break

            if step_count > limits.max_steps:
                raise StepLimitExceeded(f"Step limit exceeded: {step_count} > {limits.max_steps}")

            tool_call_count += len(response.tool_calls)
            if tool_call_count > limits.max_tool_calls:
                raise ToolCallLimitExceeded(
                    f"Tool call limit exceeded: {tool_call_count} > {limits.max_tool_calls}"
                )

            tool_messages = await self._tool_executor.execute_many(response.tool_calls)
            new_messages.extend(tool_messages)

        # Persist only the messages produced by this run; the loaded history is already in
        # the store. Re-appending the full list here was the old duplication bug.
        await self._session_store.append_messages(session_id, new_messages)
        session.version += 1
        await self._session_store.save(session)
        return response
