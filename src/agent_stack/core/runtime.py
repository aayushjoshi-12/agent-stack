from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent_stack.core.agent import Agent
from agent_stack.core.cancellation import CancellationToken
from agent_stack.core.errors import (
    AgentStackError,
    StepLimitExceeded,
    ToolCallLimitExceeded,
)
from agent_stack.core.event_bus import EventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.locks import SessionLockManager
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

    When a ``session_lock`` is wired, every run body is wrapped in the per-session lock,
    so concurrent runs against the same session serialize.

    ``run`` accepts an optional ``run_id`` (used for session creation and event emission)
    and an optional ``cancel`` token that is polled between loop iterations and around
    tool execution. Omitted, both behave exactly as before.
    """

    def __init__(
        self,
        *,
        agent: Agent,
        model: BaseChatModel,
        tool_executor: ToolExecutor,
        session_store: SessionStore,
        event_bus: EventBus | None = None,
        session_lock: SessionLockManager | None = None,
    ) -> None:
        self._agent = agent
        self._model = model
        self._tool_executor = tool_executor
        self._session_store = session_store
        self._event_bus = event_bus
        self._session_lock = session_lock

    async def run(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        run_id: str | None = None,
        cancel: CancellationToken | None = None,
    ) -> AIMessage:
        resolved_run_id = run_id or str(uuid4())

        async def body() -> AIMessage:
            return await self._run_body(
                session_id=session_id,
                user_id=user_id,
                message=message,
                run_id=resolved_run_id,
                cancel=cancel,
            )

        if self._session_lock is None:
            return await body()
        async with self._session_lock.lock(session_id):
            return await body()

    async def _run_body(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        run_id: str,
        cancel: CancellationToken | None,
    ) -> AIMessage:
        session = await self._session_store.get(session_id)
        if session is None:
            session = Session(id=session_id, owner_id=user_id, agent_name=self._agent.name)
            await self._session_store.save(session)
        stored_version = session.version

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
                self._check_cancelled(cancel)
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

                # Check again right before tool execution: a cancellation requested while
                # the model was thinking still stops the tools from running.
                self._check_cancelled(cancel)

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
            await self._session_store.save(session, expected_version=stored_version)

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

    def _check_cancelled(self, cancel: CancellationToken | None) -> None:
        if cancel is not None:
            cancel.raise_if_cancelled()

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
