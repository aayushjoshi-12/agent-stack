from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from agent_stack.core.agent import Agent
from agent_stack.core.approval import (
    Advice,
    ApprovalAdvisor,
    ApprovalBroker,
    ApprovalMode,
    ApprovalStatus,
    Verdict,
)
from agent_stack.core.cancellation import CancellationToken
from agent_stack.core.context import (
    COMPACT_PROMPT,
    FILES_HEADER,
    ContextBuilder,
    SystemPartsProvider,
    after_compaction,
    compaction_message,
    tool_schema_tokens,
)
from agent_stack.core.errors import (
    AgentStackError,
    RuntimeDeadlineExceeded,
    StepLimitExceeded,
    ToolCallLimitExceeded,
    error_text,
)
from agent_stack.core.event_bus import EventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.locks import SessionLockManager
from agent_stack.core.session import Session, SessionStore
from agent_stack.core.state import Checkpoint, CheckpointStatus
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.tools.executor import ToolExecutionResult, ToolExecutor

if TYPE_CHECKING:
    from agent_stack.persistence.base import CheckpointStore


_LOG = logging.getLogger(__name__)

#: Fewer messages than this since the last summary are not worth a compaction call.
_MIN_TO_COMPACT = 4
#: After a compaction, the files in use are re-attached: this many, each cut at _FILE_CHARS.
_FILE_TOOLS = frozenset({"read_file", "write_file", "edit_file"})
_FILES_KEPT = 5
_FILE_CHARS = 12_000
_FILES_CHARS = 40_000

#: Reasoning that says "wait" / "actually" this many times in one step is going in circles.
SPIRAL_THRESHOLD = 12
SPIRAL_NUDGE = (
    "[Your last reasoning went back and forth on the same point. Pick the best option now, "
    "act on it, and check the result by running something; do not re-derive it.]"
)
_SECOND_GUESS_RE = re.compile(r"\b(wait|actually|hmm|but wait|on second thought)\b", re.IGNORECASE)


def count_second_guessing(reasoning: str) -> int:
    """How many times a step's reasoning stops to doubt itself (a sign of a reasoning spiral)."""
    return len(_SECOND_GUESS_RE.findall(reasoning))


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


def usage_data(message: AIMessage) -> dict[str, Any] | None:
    """What one model response consumed, or ``None`` when the provider reported nothing.

    Tokens come from LangChain's normalised ``usage_metadata``; ``cost_usd`` is the provider's
    own figure when it gives one (OpenRouter puts it in ``response_metadata["cost"]``), else
    ``None`` -- an unpriced call (e.g. a local model) is counted but never guessed at.
    """
    meta: Mapping[str, Any] = message.usage_metadata or {}
    raw_cost = message.response_metadata.get("cost")
    cost = float(raw_cost) if isinstance(raw_cost, (int, float)) and raw_cost >= 0 else None
    if not meta and cost is None:
        return None
    output_details = meta.get("output_token_details") or {}
    input_details = meta.get("input_token_details") or {}
    return {
        "model": message.response_metadata.get("model_name"),
        "input_tokens": int(meta.get("input_tokens", 0)),
        "output_tokens": int(meta.get("output_tokens", 0)),
        "reasoning_tokens": int(output_details.get("reasoning", 0) or 0),
        "cache_read_tokens": int(input_details.get("cache_read", 0) or 0),
        "cache_write_tokens": int(input_details.get("cache_creation", 0) or 0),
        "cost_usd": cost,
    }


_T = TypeVar("_T")


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
        approvals: ApprovalBroker | None = None,
        advisor: ApprovalAdvisor | None = None,
        approval_mode: ApprovalMode | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self._agent = agent
        #: Where a compaction re-reads the files in use (None: it attaches none).
        self._workspace = WorkspacePolicy(workspace_root) if workspace_root else None
        self._model = model
        self._tool_executor = tool_executor
        self._session_store = session_store
        self._event_bus = event_bus
        self._session_lock = session_lock
        self._context_builder = context_builder
        #: Tokens the bound tool definitions add to every call (the message estimate omits them).
        self._tool_overhead = tool_schema_tokens(agent.tools)
        self._system_parts = system_parts
        self._approvals = approvals
        self._advisor = advisor
        self._approval_mode = approval_mode

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
        inbox: Callable[[bool], list[str]] | None = None,
        notes: str | None = None,
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
                notes=notes,
            )

        if self._session_lock is None:
            return await body()
        async with self._session_lock.lock(session_id):
            return await body()

    async def compact(self, session_id: str, focus: str | None = None) -> int:
        """Summarise a conversation now (``/compact``) and save the summary in its place.

        Returns how many messages were summarised (0: nothing since the last summary). Waits for
        a run on the session to finish first, so the marker never lands inside a run.
        """
        async with (
            self._session_lock.lock(session_id) if self._session_lock else contextlib.nullcontext()
        ):
            history = await self._session_store.get_messages(session_id)
            recent = after_compaction(history)[1]
            if not recent:
                return 0
            model_input = await self._model_input(
                resident=await self._resident_parts() if self._context_builder else (),
                history=history,
                new_messages=[],
            )
            text = await self._summarize(
                model_input, focus, run_id=f"compact-{uuid4().hex}", session_id=session_id
            )
            marker = compaction_message(text, self._files_in_use(recent))
            await self._session_store.append_messages(session_id, [marker])
        return len(recent)

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
        inbox: Callable[[bool], list[str]] | None = None,
        notes: str | None = None,
    ) -> AIMessage:
        session = await self._session_store.get(session_id)
        if session is None:
            session = Session(id=session_id, owner_id=user_id, agent_name=self._agent.name)
            await self._session_store.save(session)
        stored_version = session.version

        history = await self._session_store.get_messages(session_id)
        new_messages: list[BaseMessage] = [HumanMessage(content=message)]
        if notes:  # background-monitor notes that waited for this turn
            new_messages.append(HumanMessage(content=notes))

        # Bound what the model sees. Resident system parts are computed once per run;
        # the message window is rebuilt each iteration as tool results accumulate.
        resident: Sequence[str] = ()
        if self._context_builder is not None:
            resident = await self._resident_parts()

        limits = self._agent.limits
        deadline = time.monotonic() + limits.max_runtime_seconds
        step_count = 0
        tool_call_count = 0
        spiral_nudge = False
        summary_memo: dict[str, Any] = {}

        async def take_input(*, close: bool) -> bool:
            """Append the inbox's waiting messages to the run; True when there were any."""
            texts = inbox(close) if inbox is not None else []
            for text in texts:
                new_messages.append(HumanMessage(content=text))
                await self._emit(
                    EventType.INPUT_RECEIVED,
                    run_id=run_id,
                    session_id=session_id,
                    parent_run_id=parent_run_id,
                    data={"text": text},
                )
            return bool(texts)

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
                # Cooperative input injection (P20): drain anything the parent (or the user,
                # mid-run) pushed at this boundary — right before the model call, which also
                # covers the gap between tool batches. The callable is the seam; no asyncio here.
                await take_input(close=False)
                if spiral_nudge:
                    spiral_nudge = False
                    new_messages.append(HumanMessage(content=SPIRAL_NUDGE))
                await self._maybe_compact(
                    resident=resident,
                    history=history,
                    new_messages=new_messages,
                    memo=summary_memo,
                    run_id=run_id,
                    session_id=session_id,
                    parent_run_id=parent_run_id,
                    deadline=deadline,
                )
                model_input = await self._model_input(
                    resident=resident, history=history, new_messages=new_messages, memo=summary_memo
                )
                response = await self._within(deadline, self._invoker(model_input))
                if not isinstance(response, AIMessage):
                    raise AgentStackError(
                        f"Model returned {type(response).__name__}, expected AIMessage"
                    )
                new_messages.append(response)
                step_count += 1

                usage = usage_data(response)
                if usage is not None and self._context_builder is not None:
                    self._context_builder.calibrate(
                        summary_memo["raw_estimate"],
                        int(usage["input_tokens"]),
                        self._tool_overhead,
                    )
                if usage is not None:
                    await self._emit(
                        EventType.MODEL_USAGE,
                        run_id=run_id,
                        session_id=session_id,
                        parent_run_id=parent_run_id,
                        data=usage,
                    )

                reasoning = reasoning_text(response)
                if count_second_guessing(reasoning) >= SPIRAL_THRESHOLD:
                    spiral_nudge = True
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
                    # A message that arrived during this last step is answered in this run;
                    # with none waiting the inbox closes, so a late one is refused, never lost.
                    if await take_input(close=True):
                        continue
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
                        data={
                            "tool": tool_call["name"],
                            "tool_call_id": tool_call["id"],
                            "args": dict(tool_call["args"]),
                        },
                    )

                # Check again right before tool execution: a cancellation requested while
                # the model was thinking still stops the tools from running.
                self._check_cancelled(cancel)

                # Human-in-the-loop (P26): gated calls wait for a decision, one at a time.
                # Rejected calls never execute; they become error ToolMessages. The time a
                # human takes is not the run's time, so it is added back to the deadline.
                rejected: dict[str, ToolExecutionResult] = {}
                asked = time.monotonic()
                for gated in pending:
                    if not self._agent.tool_policy.needs_approval(
                        str(gated["name"]), gated["args"]
                    ):
                        continue
                    note: str | None = None
                    if self._advisor is not None and (
                        self._approval_mode is not None and self._approval_mode.value == "auto"
                    ):
                        advice = await self._advise(
                            gated,
                            request=_request_text(new_messages),
                            run_id=run_id,
                            session_id=session_id,
                            parent_run_id=parent_run_id,
                        )
                        if advice.verdict is Verdict.ALLOW:
                            continue
                        if advice.verdict is Verdict.DENY:
                            rejected[str(gated["id"])] = _rejected_result(
                                gated,
                                f"auto mode blocked this call: {advice.reason}. "
                                "Do not retry it unchanged; do something safer or ask the user.",
                            )
                            continue
                        note = advice.reason  # ASK: the human decides, told why
                    if self._approvals is None:
                        # Fail closed: a gated tool with nobody to ask must not run.
                        rejected[str(gated["id"])] = _rejected_result(
                            gated, "this tool needs approval and no approval channel is configured"
                        )
                        continue
                    status = await self._await_approval(
                        gated,
                        run_id=run_id,
                        session_id=session_id,
                        user_id=user_id,
                        parent_run_id=parent_run_id,
                        note=note,
                    )
                    if status is not ApprovalStatus.APPROVED:
                        rejected[str(gated["id"])] = _rejected_result(gated)
                deadline += time.monotonic() - asked
                self._check_cancelled(cancel)
                runnable = [c for c in pending if str(c["id"]) not in rejected]

                def run_tools(batch: list[Any] = runnable) -> Awaitable[list[Any]]:
                    return self._tool_executor.execute_many(
                        batch,
                        on_output=self._tool_output_emitter(
                            run_id=run_id,
                            session_id=session_id,
                            parent_run_id=parent_run_id,
                            tool_calls=batch,
                        ),
                    )

                executed = await self._within(deadline, run_tools)
                by_id = {r.tool_call_id: r for r in [*executed, *rejected.values()]}
                tool_results = [by_id[str(c["id"])] for c in pending]

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
                            "ok": result.success,
                            "error": result.error,
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
            await self._save_session(session, stored_version)

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
        except asyncio.CancelledError:
            # Hard cancel: finish saving what happened even though this task is being torn down.
            await asyncio.shield(
                self._persist_partial(session, stored_version, session_id, new_messages)
            )
            raise
        except Exception as exc:
            await self._persist_partial(session, stored_version, session_id, new_messages)
            await self._emit(
                EventType.RUN_FAILED,
                run_id=run_id,
                session_id=session_id,
                parent_run_id=parent_run_id,
                data={"error": error_text(exc)},
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

    def _invoker(self, model_input: list[BaseMessage]) -> Callable[[], Awaitable[Any]]:
        """A zero-argument call of the model on ``model_input`` (binds it, not the loop variable)."""
        return lambda: self._model.ainvoke(model_input)

    async def _maybe_compact(
        self,
        *,
        resident: Sequence[str],
        history: Sequence[BaseMessage],
        new_messages: list[BaseMessage],
        memo: dict[str, Any],
        run_id: str,
        session_id: str,
        parent_run_id: str | None,
        deadline: float,
    ) -> None:
        """Past the threshold, replace the conversation so far with one saved summary (P31).

        The summary is appended to ``new_messages`` as a marker, so it is saved with the run; the
        model sees only the latest marker and what follows. One failure turns it off for the run:
        dropping old messages (``ContextBuilder.build``) still keeps the context in budget.
        """
        builder = self._context_builder
        if builder is None or memo.get("compact_failed"):
            return
        summary, recent = after_compaction([*history, *new_messages])
        if len(recent) < _MIN_TO_COMPACT or not builder.over_threshold(
            _with_summary(resident, summary), recent, self._tool_overhead
        ):
            return
        model_input = await self._model_input(
            resident=resident, history=history, new_messages=new_messages
        )
        try:
            text = await self._within(
                deadline,
                lambda: self._summarize(
                    model_input,
                    None,
                    run_id=run_id,
                    session_id=session_id,
                    parent_run_id=parent_run_id,
                ),
            )
        except RuntimeDeadlineExceeded:
            raise
        except Exception:  # a failed summary must never stop the run
            memo["compact_failed"] = True
            _LOG.warning("compacting the conversation failed", exc_info=True)
            return
        new_messages.append(compaction_message(text, self._files_in_use(recent)))
        await self._emit(
            EventType.CONTEXT_COMPACTED,
            run_id=run_id,
            session_id=session_id,
            parent_run_id=parent_run_id,
            data={"messages": len(recent), "auto": True},
        )

    async def _summarize(
        self,
        model_input: list[BaseMessage],
        focus: str | None,
        *,
        run_id: str,
        session_id: str,
        parent_run_id: str | None = None,
    ) -> str:
        """Ask this conversation's own model for the summary, the way Claude Code does.

        The model gets exactly what it was about to see (so a prompt cache can reuse it) with
        :data:`COMPACT_PROMPT` added at the end. Its usage is recorded like any other call.
        """
        ask = COMPACT_PROMPT + (
            f"\n\nThe user asked the summary to focus on: {focus}" if focus else ""
        )
        reply = await self._model.ainvoke([*model_input, HumanMessage(content=ask)])
        if not isinstance(reply, AIMessage):
            raise AgentStackError(f"Model returned {type(reply).__name__}, expected AIMessage")
        usage = usage_data(reply)
        if usage is not None:
            await self._emit(
                EventType.MODEL_USAGE,
                run_id=run_id,
                session_id=session_id,
                parent_run_id=parent_run_id,
                data=usage,
            )
        text = str(reply.text).strip()
        if not text:
            raise AgentStackError("the model returned no summary")
        return text

    def _files_in_use(self, messages: Sequence[BaseMessage]) -> str:
        """The files the summarised messages read or edited, newest first, as they are now.

        Claude Code re-attaches recently read files after a compaction, so the next step does not
        start by re-reading them. Paths outside the workspace and unreadable files are skipped.
        """
        if self._workspace is None:
            return ""
        paths: list[str] = []
        for message in reversed(messages):
            for call in reversed(getattr(message, "tool_calls", None) or ()):
                path = (call.get("args") or {}).get("path")
                if call.get("name") in _FILE_TOOLS and isinstance(path, str) and path not in paths:
                    paths.append(path)
        blocks: list[str] = []
        used = 0
        for path in paths[:_FILES_KEPT]:
            try:
                text = self._workspace.resolve(path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError, AgentStackError):
                continue
            if len(text) > _FILE_CHARS:
                text = (
                    text[:_FILE_CHARS] + f"\n…[{len(text) - _FILE_CHARS} more chars; read_file it]"
                )
            if used + len(text) > _FILES_CHARS:
                break
            used += len(text)
            blocks.append(f"## {path}\n```\n{text}\n```")
        return "\n\n".join(blocks)

    async def _model_input(
        self,
        *,
        resident: Sequence[str],
        history: Sequence[BaseMessage],
        new_messages: Sequence[BaseMessage],
        memo: dict[str, Any] | None = None,
    ) -> list[BaseMessage]:
        """Build one model input: system parts plus the (windowed) messages.

        Only the latest compaction summary and the messages after it are shown; the summary
        joins the system parts. Unwired (no builder), the base prompt goes over those messages
        as they are; with a builder the parts are budgeted and the messages windowed.
        """
        summary, messages = after_compaction([*history, *new_messages])
        if summary is not None and FILES_HEADER in summary:
            # the files ride as the first message: the system part stays small, and under
            # pressure they are the first thing dropped
            summary, _, files = summary.partition(FILES_HEADER)
            messages = [HumanMessage(content=FILES_HEADER + files), *messages]
            summary = summary.rstrip()
        if self._context_builder is None:
            system = "\n\n".join(_with_summary([self._agent.system_prompt], summary))
            return [SystemMessage(content=system), *messages]
        pinned = new_messages[0] if new_messages else None
        ctx = self._context_builder.build(
            system_parts=_with_summary(resident, summary),
            history=messages,
            pinned=pinned,
            overhead=self._tool_overhead,
        )
        if memo is not None:
            memo["raw_estimate"] = ctx.raw_estimate  # per run: concurrent runs share this runtime
        return [SystemMessage(content="\n\n".join(ctx.system)), *ctx.messages]

    async def _save_session(self, session: Session, stored_version: int) -> None:
        """Save the session at the end of a run without undoing a rename made during it.

        The run holds the session in memory from its start and writes every column back, so a
        title changed meanwhile (a user rename, the small model's title) would be overwritten by
        the stale copy. The stored title is therefore re-read and kept.
        """
        latest = await self._session_store.get(session.id)
        if latest is not None and latest.title != session.title:
            session.title = latest.title
        await self._session_store.save(session, expected_version=stored_version)

    async def _persist_partial(
        self,
        session: Session,
        stored_version: int,
        session_id: str,
        messages: Sequence[BaseMessage],
    ) -> None:
        """Save what a failed or cancelled run produced, so history keeps the user's message.

        A trailing assistant message whose tool calls never got results is dropped: stored
        as is, the next model call would be rejected for tool calls without responses. This
        is best effort and never replaces the original error with a storage error.
        """
        keep = list(messages)
        if keep and isinstance(keep[-1], AIMessage) and keep[-1].tool_calls:
            keep.pop()
        if not keep:
            return
        with contextlib.suppress(Exception):
            await self._session_store.append_messages(session_id, keep)
            session.version += 1
            await self._save_session(session, stored_version)

    async def _advise(
        self,
        tool_call: Mapping[str, Any],
        *,
        request: str | None,
        run_id: str,
        session_id: str,
        parent_run_id: str | None,
    ) -> Advice:
        """Ask the advisor about a gated call and announce its verdict. Never raises: an advisor
        that fails or hangs leaves the decision to the human."""
        assert self._advisor is not None
        try:
            advice = await asyncio.wait_for(
                self._advisor.advise(
                    str(tool_call["name"]), dict(tool_call["args"]), request=request
                ),
                timeout=_ADVISOR_SECONDS,
            )
        except Exception as exc:
            advice = Advice(Verdict.ASK, f"auto mode could not decide ({type(exc).__name__})")
        await self._emit(
            EventType.APPROVAL_AUTO,
            run_id=run_id,
            session_id=session_id,
            parent_run_id=parent_run_id,
            data={
                "tool": str(tool_call["name"]),
                "tool_call_id": str(tool_call["id"]),
                "verdict": advice.verdict.value,
                "reason": advice.reason,
            },
        )
        return advice

    async def _await_approval(
        self,
        tool_call: Mapping[str, Any],
        *,
        run_id: str,
        session_id: str,
        user_id: str,
        parent_run_id: str | None,
        note: str | None = None,
    ) -> ApprovalStatus:
        """Announce a gated call, wait for its decision, and announce the outcome."""
        assert self._approvals is not None
        request = self._approvals.create(
            run_id=run_id,
            session_id=session_id,
            principal=user_id,
            tool=str(tool_call["name"]),
            tool_call_id=str(tool_call["id"]),
            args=dict(tool_call["args"]),
        )
        await self._emit(
            EventType.APPROVAL_REQUIRED,
            run_id=run_id,
            session_id=session_id,
            parent_run_id=parent_run_id,
            data={
                "approval_id": request.id,
                "tool": request.tool,
                "tool_call_id": request.tool_call_id,
                "args": dict(request.args),
                **({"note": note} if note else {}),
            },
        )
        status = await self._approvals.wait(request.id)
        await self._emit(
            EventType.APPROVAL_RESOLVED,
            run_id=run_id,
            session_id=session_id,
            parent_run_id=parent_run_id,
            data={
                "approval_id": request.id,
                "tool_call_id": request.tool_call_id,
                "status": status.value,
            },
        )
        return status

    async def _within(self, deadline: float, factory: Callable[[], Awaitable[_T]]) -> _T:
        """Await ``factory()`` but never past the run's wall-clock ``deadline``.

        The awaitable is built lazily so an already-expired deadline never leaves a
        never-awaited coroutine behind. Only the timeout *this* scope set is converted;
        a ``TimeoutError`` raised by the provider itself propagates unchanged.
        """
        remaining = deadline - time.monotonic()
        limit = self._agent.limits.max_runtime_seconds
        if remaining <= 0:
            raise RuntimeDeadlineExceeded(f"Run exceeded its {limit}s runtime limit")
        scope = asyncio.timeout(remaining)
        try:
            async with scope:
                return await factory()
        except TimeoutError as exc:
            if scope.expired():
                raise RuntimeDeadlineExceeded(f"Run exceeded its {limit}s runtime limit") from exc
            raise

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


#: An advisor slower than this counts as "could not decide" and the human is asked.
_ADVISOR_SECONDS = 45.0


def _request_text(messages: Sequence[BaseMessage]) -> str | None:
    """What the user asked for in this run (its first message), shortened for an advisor."""
    for message in messages:
        if isinstance(message, HumanMessage):
            return str(message.content)[:2000]
    return None


def _rejected_result(
    tool_call: Mapping[str, Any], reason: str = "the user rejected this tool call"
) -> ToolExecutionResult:
    """The outcome of a call that did not get approved: an error the model can read."""
    call_id = str(tool_call["id"])
    return ToolExecutionResult(
        tool_call_id=call_id,
        tool_name=str(tool_call["name"]),
        message=ToolMessage(content=f"Error: {reason}", tool_call_id=call_id, status="error"),
        success=False,
        error=reason,
    )


def _with_summary(parts: Sequence[str], summary: str | None) -> list[str]:
    """The system parts with a compaction summary right after the base prompt (never cut first)."""
    if summary is None:
        return list(parts)
    return [*parts[:1], summary, *parts[1:]]
