from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from agent_stack.core.agent import Agent
from agent_stack.core.cancellation import CancellationToken
from agent_stack.core.context import ContextBuilder, SystemPartsProvider
from agent_stack.core.errors import (
    AgentStackError,
    StepLimitExceeded,
    ToolCallLimitExceeded,
)
from agent_stack.core.event_bus import EventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.locks import SessionLockManager
from agent_stack.core.session import Session, SessionStore
from agent_stack.core.state import Checkpoint, CheckpointStatus
from agent_stack.tools.executor import ToolExecutor

if TYPE_CHECKING:
    from agent_stack.persistence.base import CheckpointStore


def reasoning_text(message: AIMessage) -> str:
    """The model's thinking/reasoning for a step, or ``""`` when the provider gave none.

    Prefers LangChain's normalised ``reasoning`` content blocks and falls back to the
    ``reasoning_content`` key that OpenAI-compatible providers (GLM, DeepSeek, …) put in
    ``additional_kwargs``. Never raises: an unreadable content shape yields no reasoning.
    """
    try:
        blocks: Sequence[Any] = message.content_blocks
    except Exception:
        blocks = ()
    parts = [
        str(block["reasoning"])
        for block in blocks
        if isinstance(block, Mapping)
        and block.get("type") == "reasoning"
        and block.get("reasoning")
    ]
    if parts:
        return "\n\n".join(parts)
    raw = message.additional_kwargs.get("reasoning_content")
    return raw if isinstance(raw, str) else ""


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

    ``run`` accepts an optional ``run_id`` (used for session creation and event emission),
    an optional ``cancel`` token that is polled between loop iterations and around
    tool execution, and an optional ``counters`` callback that receives the run's final
    ``(step_count, tool_call_count)`` on success. Omitted, all behave exactly as before.

    An optional ``checkpoint_store`` on ``run`` makes the loop durable: it writes
    semantic checkpoints (AI message accepted, tool batch completed, run completed) and
    records every executed tool call under the ``(run_id, tool_call_id)`` idempotency key,
    so a resumed run skips calls that already completed instead of re-executing them.
    With no store, the loop behaves exactly as before.

    An optional ``context_builder`` bounds what the model sees: the loaded history is
    ``compact``ed once, ``system_parts`` supplies the ordered resident system blocks, and
    each iteration's input is built through the builder (system parts joined into one
    ``SystemMessage`` followed by the recent window of history + new messages). With both
    omitted, the loop sends the base prompt over the raw messages exactly as before.
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
        context_builder: ContextBuilder | None = None,
        system_parts: SystemPartsProvider | None = None,
    ) -> None:
        self._agent = agent
        self._model = model
        self._tool_executor = tool_executor
        self._session_store = session_store
        self._event_bus = event_bus
        self._session_lock = session_lock
        self._context_builder = context_builder
        self._system_parts = system_parts

    @property
    def agent(self) -> Agent:
        """The agent definition this runtime executes (used for delegation linkage)."""
        return self._agent

    async def run(
        self,
        *,
        session_id: str,
        user_id: str,
        message: str,
        run_id: str | None = None,
        cancel: CancellationToken | None = None,
        counters: Callable[[int, int], None] | None = None,
        checkpoint_store: CheckpointStore | None = None,
        parent_run_id: str | None = None,
        inbox: Callable[[], list[str]] | None = None,
    ) -> AIMessage:
        resolved_run_id = run_id or str(uuid4())

        async def body() -> AIMessage:
            return await self._run_body(
                session_id=session_id,
                user_id=user_id,
                message=message,
                run_id=resolved_run_id,
                cancel=cancel,
                counters=counters,
                checkpoint_store=checkpoint_store,
                parent_run_id=parent_run_id,
                inbox=inbox,
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
        counters: Callable[[int, int], None] | None,
        checkpoint_store: CheckpointStore | None,
        parent_run_id: str | None = None,
        inbox: Callable[[], list[str]] | None = None,
    ) -> AIMessage:
        session = await self._session_store.get(session_id)
        if session is None:
            session = Session(id=session_id, owner_id=user_id, agent_name=self._agent.name)
            await self._session_store.save(session)
        stored_version = session.version

        history = await self._session_store.get_messages(session_id)
        new_messages: list[BaseMessage] = [HumanMessage(content=message)]

        # Bound what the model sees. Resident system parts are computed once per run;
        # the message window is rebuilt each iteration as tool results accumulate.
        resident: Sequence[str] = ()
        if self._context_builder is not None:
            resident = await self._resident_parts()
            compacted = await self._context_builder.compact(history)
            # A summarizer prepends the compaction summary as a SystemMessage; lift it
            # into the resident parts so re-windowing below cannot drop it.
            if compacted and isinstance(compacted[0], SystemMessage):
                resident = (*resident, str(compacted[0].content))
                compacted = compacted[1:]
            history = compacted

        limits = self._agent.limits
        step_count = 0
        tool_call_count = 0

        await self._emit(
            EventType.RUN_STARTED,
            run_id=run_id,
            session_id=session_id,
            parent_run_id=parent_run_id,
            data={"agent": self._agent.name, "user_id": user_id},
        )

        try:
            while True:
                self._check_cancelled(cancel)
                # Cooperative input injection (P20): drain anything the parent pushed at
                # this boundary — right before the model call, which also covers the gap
                # between tool batches. The callable is the seam; no asyncio here.
                if inbox is not None:
                    for text in inbox():
                        new_messages.append(HumanMessage(content=text))
                response = await self._model.ainvoke(
                    self._model_input(resident=resident, history=history, new_messages=new_messages)
                )
                if not isinstance(response, AIMessage):
                    raise AgentStackError(
                        f"Model returned {type(response).__name__}, expected AIMessage"
                    )
                new_messages.append(response)
                step_count += 1

                reasoning = reasoning_text(response)
                if reasoning:
                    await self._emit(
                        EventType.MODEL_REASONING,
                        run_id=run_id,
                        session_id=session_id,
                        parent_run_id=parent_run_id,
                        data={"text": reasoning},
                    )

                await self._emit(
                    EventType.ASSISTANT_MESSAGE,
                    run_id=run_id,
                    session_id=session_id,
                    parent_run_id=parent_run_id,
                    data={
                        "content": response.content,
                        "tool_calls": [tool_call["name"] for tool_call in response.tool_calls],
                    },
                )

                await self._checkpoint(
                    checkpoint_store,
                    run_id=run_id,
                    session_id=session_id,
                    step=step_count,
                    tool_call_count=tool_call_count,
                    status=CheckpointStatus.AI_MESSAGE,
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

                # Idempotency: calls already recorded for this run (by a previous attempt
                # that crashed mid-loop) are skipped, so a mutating tool never runs twice.
                completed_ids = (
                    await checkpoint_store.completed_tool_call_ids(run_id)
                    if checkpoint_store is not None
                    else set()
                )
                pending = [
                    tool_call
                    for tool_call in response.tool_calls
                    if tool_call["id"] not in completed_ids
                ]

                for tool_call in pending:
                    await self._emit(
                        EventType.TOOL_STARTED,
                        run_id=run_id,
                        session_id=session_id,
                        parent_run_id=parent_run_id,
                        data={"tool": tool_call["name"], "tool_call_id": tool_call["id"]},
                    )

                # Check again right before tool execution: a cancellation requested while
                # the model was thinking still stops the tools from running.
                self._check_cancelled(cancel)

                tool_results = await self._tool_executor.execute_many(
                    pending,
                    on_output=self._tool_output_emitter(
                        run_id=run_id,
                        session_id=session_id,
                        parent_run_id=parent_run_id,
                        tool_calls=pending,
                    ),
                )

                if checkpoint_store is not None and tool_results:
                    # The whole batch lands in one transaction, atomically visible to recovery.
                    await checkpoint_store.record_tool_executions(run_id, tool_results)

                for result in tool_results:
                    await self._emit(
                        EventType.TOOL_COMPLETED,
                        run_id=run_id,
                        session_id=session_id,
                        parent_run_id=parent_run_id,
                        data={
                            "tool": result.tool_name,
                            "tool_call_id": result.tool_call_id,
                        },
                    )

                # Project executed results back into the conversation in the model's call
                # order; skipped calls get a placeholder (SQLite holds no tool content to
                # replay, so the previous result is not reconstructed here).
                result_by_id = {result.tool_call_id: result.message for result in tool_results}
                for tool_call in response.tool_calls:
                    tool_call_id = tool_call["id"]
                    if tool_call_id is None:
                        raise AgentStackError("Model returned a tool call without an id")
                    tool_message = result_by_id.get(tool_call_id)
                    if tool_message is None:
                        tool_message = ToolMessage(
                            content="(skipped: already completed in a previous attempt of this run)",
                            tool_call_id=tool_call_id,
                        )
                    new_messages.append(tool_message)

                if tool_results:
                    await self._checkpoint(
                        checkpoint_store,
                        run_id=run_id,
                        session_id=session_id,
                        step=step_count,
                        tool_call_count=tool_call_count,
                        status=CheckpointStatus.TOOL_BATCH_COMPLETED,
                    )

            # Persist only the messages produced by this run; the loaded history is already
            # in the store. Re-appending the full list here was the old duplication bug.
            await self._session_store.append_messages(session_id, new_messages)
            session.version += 1
            await self._session_store.save(session, expected_version=stored_version)

            await self._checkpoint(
                checkpoint_store,
                run_id=run_id,
                session_id=session_id,
                step=step_count,
                tool_call_count=tool_call_count,
                status=CheckpointStatus.RUN_COMPLETED,
            )

            await self._emit(
                EventType.RUN_COMPLETED,
                run_id=run_id,
                session_id=session_id,
                parent_run_id=parent_run_id,
                data={"steps": step_count, "tool_calls": tool_call_count},
            )
        except Exception as exc:
            await self._emit(
                EventType.RUN_FAILED,
                run_id=run_id,
                session_id=session_id,
                parent_run_id=parent_run_id,
                data={"error": repr(exc)},
            )
            raise

        if counters is not None:
            counters(step_count, tool_call_count)
        return response

    async def _resident_parts(self) -> Sequence[str]:
        """The ordered resident system parts, or just the base prompt when unwired."""
        if self._system_parts is None:
            return (self._agent.system_prompt,)
        return await self._system_parts.parts(base_prompt=self._agent.system_prompt)

    def _model_input(
        self,
        *,
        resident: Sequence[str],
        history: Sequence[BaseMessage],
        new_messages: Sequence[BaseMessage],
    ) -> list[BaseMessage]:
        """Build one model input: system parts plus the (windowed) messages.

        Unwired (no builder), this is exactly the legacy single ``SystemMessage`` over
        the raw messages. With a builder the system parts are joined into one
        ``SystemMessage`` and the messages are the recent window of ``history + new``.
        """
        if self._context_builder is None:
            return [SystemMessage(content=self._agent.system_prompt), *history, *new_messages]
        ctx = self._context_builder.build(system_parts=resident, history=[*history, *new_messages])
        return [SystemMessage(content="\n\n".join(ctx.system)), *ctx.messages]

    def _check_cancelled(self, cancel: CancellationToken | None) -> None:
        if cancel is not None:
            cancel.raise_if_cancelled()

    def _tool_output_emitter(
        self,
        *,
        run_id: str,
        session_id: str,
        parent_run_id: str | None,
        tool_calls: Sequence[Mapping[str, Any]],
    ) -> Callable[[str, str, str], Awaitable[None]] | None:
        """Build the batch's live-output hook, or ``None`` when no bus is wired.

        With no bus there is no sink, so streaming tools fall back to their unchanged
        (no live output) behaviour.
        """
        if self._event_bus is None:
            return None

        names = {str(tool_call["id"]): str(tool_call["name"]) for tool_call in tool_calls}

        async def emit(tool_call_id: str, stream: str, chunk: str) -> None:
            await self._emit(
                EventType.TOOL_OUTPUT,
                run_id=run_id,
                session_id=session_id,
                parent_run_id=parent_run_id,
                data={
                    "tool": names.get(tool_call_id),
                    "tool_call_id": tool_call_id,
                    "stream": stream,
                    "chunk": chunk,
                },
            )

        return emit

    async def _emit(
        self,
        event_type: EventType,
        *,
        run_id: str,
        session_id: str,
        data: Mapping[str, Any],
        parent_run_id: str | None = None,
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
            parent_run_id=parent_run_id,
        )
        with contextlib.suppress(Exception):
            await self._event_bus.publish(event)

    async def _checkpoint(
        self,
        checkpoint_store: CheckpointStore | None,
        *,
        run_id: str,
        session_id: str,
        step: int,
        tool_call_count: int,
        status: CheckpointStatus,
    ) -> None:
        """Persist a checkpoint at a loop boundary when a store is wired."""
        if checkpoint_store is None:
            return
        await checkpoint_store.save(
            Checkpoint(
                run_id=run_id,
                session_id=session_id,
                step=step,
                tool_call_count=tool_call_count,
                status=status,
                created_at=datetime.now(UTC).isoformat(),
            )
        )
