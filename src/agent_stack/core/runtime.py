from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent_stack.core.agent import Agent
from agent_stack.core.errors import (
    AgentStackError,
    StepLimitExceeded,
    ToolCallLimitExceeded,
)
from agent_stack.core.event_bus import EventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.session import Session, SessionStore
from agent_stack.tools.executor import ToolExecutor


class AgentRuntime:
    """The explicit model/tool loop.

    The loop is deliberately boring: build a context, ask the model, execute any tool
    calls, repeat until the model returns a final answer. Persistence, limits, events and
    subagents surround this loop; the loop itself does not grow branches for them.

    The ``model`` is expected to be already bound to the agent's tools by ``AgentFactory``.

    When an ``event_bus`` is wired, the runtime emits the run lifecycle as
    :class:`AgentEvent`s (:data:`~EventType.RUN_STARTED`, ``ASSISTANT_MESSAGE``,
    ``TOOL_STARTED``/``TOOL_COMPLETED``, ``RUN_COMPLETED``/``RUN_FAILED``). With no bus,
    the loop behaves exactly as before.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        model: BaseChatModel,
        tool_executor: ToolExecutor,
        session_store: SessionStore,
        event_bus: EventBus | None = None,
    ) -> None:
        self._agent = agent
        self._model = model
        self._tool_executor = tool_executor
        self._session_store = session_store
        self._event_bus = event_bus

    async def run(self, *, session_id: str, user_id: str, message: str) -> AIMessage:
        run_id = str(uuid4())

        session = await self._session_store.get(session_id)
        if session is None:
            session = Session(id=session_id, owner_id=user_id, agent_name=self._agent.name)
            await self._session_store.save(session)

        history = await self._session_store.get_messages(session_id)
        new_messages: list[BaseMessage] = [HumanMessage(content=message)]

        limits = self._agent.limits
        step_count = 0
        tool_call_count = 0

        await self._emit(
            EventType.RUN_STARTED,
            run_id=run_id,
            session_id=session_id,
            data={"agent": self._agent.name, "user_id": user_id},
        )

        try:
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

                await self._emit(
                    EventType.ASSISTANT_MESSAGE,
                    run_id=run_id,
                    session_id=session_id,
                    data={
                        "content": response.content,
                        "tool_calls": [tool_call["name"] for tool_call in response.tool_calls],
                    },
                )

                if not response.tool_calls:
                    break

                if step_count > limits.max_steps:
                    raise StepLimitExceeded(
                        f"Step limit exceeded: {step_count} > {limits.max_steps}"
                    )

                tool_call_count += len(response.tool_calls)
                if tool_call_count > limits.max_tool_calls:
                    raise ToolCallLimitExceeded(
                        f"Tool call limit exceeded: {tool_call_count} > {limits.max_tool_calls}"
                    )

                for tool_call in response.tool_calls:
                    await self._emit(
                        EventType.TOOL_STARTED,
                        run_id=run_id,
                        session_id=session_id,
                        data={"tool": tool_call["name"], "tool_call_id": tool_call["id"]},
                    )

                tool_messages = await self._tool_executor.execute_many(response.tool_calls)
                new_messages.extend(tool_messages)

                for tool_call in response.tool_calls:
                    await self._emit(
                        EventType.TOOL_COMPLETED,
                        run_id=run_id,
                        session_id=session_id,
                        data={"tool": tool_call["name"], "tool_call_id": tool_call["id"]},
                    )

            # Persist only the messages produced by this run; the loaded history is already
            # in the store. Re-appending the full list here was the old duplication bug.
            await self._session_store.append_messages(session_id, new_messages)
            session.version += 1
            await self._session_store.save(session)

            await self._emit(
                EventType.RUN_COMPLETED,
                run_id=run_id,
                session_id=session_id,
                data={"steps": step_count, "tool_calls": tool_call_count},
            )
        except Exception as exc:
            await self._emit(
                EventType.RUN_FAILED,
                run_id=run_id,
                session_id=session_id,
                data={"error": repr(exc)},
            )
            raise

        return response

    async def _emit(
        self,
        event_type: EventType,
        *,
        run_id: str,
        session_id: str,
        data: Mapping[str, Any],
    ) -> None:
        """Publish an event if a bus is wired.

        A failing bus must never break a run: publishing is best-effort and any error it
        raises is swallowed.
        """
        if self._event_bus is None:
            return

        event = AgentEvent.create(
            event_type,
            run_id=run_id,
            session_id=session_id,
            sequence=0,
            data=data,
        )
        with contextlib.suppress(Exception):
            await self._event_bus.publish(event)
