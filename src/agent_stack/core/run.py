"""Run lifecycle: ``Run`` / ``RunStatus`` plus the run manager and handle.

A :class:`Run` owns execution *lifecycle* only — never conversation content. The
:class:`RunManager` turns a :class:`RunRequest` into a started run, exposes a live
:class:`RunHandle` (events + result), and can cancel or recover runs. The manager is the
only place that drives the runtime with a ``run_id`` and a cancellation token.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain_core.messages import AIMessage

from agent_stack.core.cancellation import CancellationToken
from agent_stack.core.errors import RunCancelled, error_text
from agent_stack.core.event_bus import EventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.run_context import RunContext, RunView, SubagentRun, run_context
from agent_stack.core.runtime import AgentRuntime

if TYPE_CHECKING:
    from agent_stack.core.agent import Agent
    from agent_stack.core.factory import AgentFactory
    from agent_stack.persistence.base import CheckpointStore, RunStore

__all__ = ["Run", "RunHandle", "RunManager", "RunRequest", "RunStatus"]

_TERMINAL_EVENTS = frozenset({EventType.RUN_COMPLETED, EventType.RUN_FAILED})


class RunStatus(StrEnum):
    """Lifecycle states of a single agent run."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})
#: Runs that have not finished: after a crash they are all stale, and a run paused for a
#: human's approval is as live as a running one (it still holds its session and its slot).
LIVE_RUN_STATUSES = frozenset({RunStatus.PENDING, RunStatus.RUNNING, RunStatus.WAITING_APPROVAL})


@dataclass
class Run:
    """A single execution of an agent against a session.

    ``Run`` owns the execution *lifecycle*; it never owns conversation content.
    """

    id: str
    session_id: str
    status: RunStatus = RunStatus.PENDING
    step_count: int = 0
    tool_call_count: int = 0
    error: str | None = None
    parent_run_id: str | None = None


@dataclass(frozen=True)
class RunRequest:
    """Everything needed to start a run."""

    session_id: str
    user_id: str
    message: str
    parent_run_id: str | None = None
    depth: int = 0
    #: Tool-made text (monitor notes) the model reads as a message after ``message``. Kept apart
    #: so ``message`` stays the user's own words, which the auto-mode classifier sees.
    notes: str | None = None


@dataclass
class _Progress:
    """Live counters derived from a run's event stream (before the run is persisted)."""

    steps: int = 0
    tools: int = 0
    last_event: str | None = None


class RunHandle:
    """A started run: its live event stream and its eventual outcome."""

    def __init__(
        self,
        run: Run,
        events: AsyncIterator[AgentEvent],
        result: Awaitable[AIMessage],
    ) -> None:
        self._run = run
        self._events = events
        self._result = result

    @property
    def run_id(self) -> str:
        return self._run.id

    def events(self) -> AsyncIterator[AgentEvent]:
        """The run's live event stream (single-consumption, yields until the run ends)."""
        return self._events

    async def result(self) -> AIMessage:
        """Await the run's outcome, re-raising a failure or cancellation."""
        return await self._result


class RunManager:
    """Starts, cancels, and recovers runs against a runtime and a run store."""

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        run_store: RunStore,
        event_bus: EventBus | None = None,
        checkpoint_store: CheckpointStore | None = None,
        agent_factory: AgentFactory | None = None,
    ) -> None:
        self._runtime = runtime
        self._run_store = run_store
        self._event_bus = event_bus
        self._checkpoint_store = checkpoint_store
        self._agent_factory = agent_factory

        self._tasks: dict[str, asyncio.Task[AIMessage]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._queues: dict[str, asyncio.Queue[AgentEvent | None]] = {}
        self._inboxes: dict[str, asyncio.Queue[str]] = {}
        self._progress: dict[str, _Progress] = {}
        self._agent_names: dict[str, str] = {}

    @property
    def runtime(self) -> AgentRuntime:
        """The runtime new runs start on."""
        return self._runtime

    def use_runtime(self, runtime: AgentRuntime) -> None:
        """Start later runs on ``runtime`` (a model switch); running ones keep theirs."""
        self._runtime = runtime

    async def start(
        self,
        request: RunRequest,
        *,
        runtime: AgentRuntime | None = None,
    ) -> RunHandle:
        """Start a run against the wired runtime (or an explicit override).

        The override is how a subagent run executes against *its own* agent runtime;
        everything else (persistence, events, cancellation, inbox) is identical.
        """
        return await self._start(request, runtime or self._runtime)

    async def _start(self, request: RunRequest, runtime: AgentRuntime) -> RunHandle:
        """Persist the run, subscribe for its events, and start executing it."""
        run = Run(
            id=uuid4().hex,
            session_id=request.session_id,
            parent_run_id=request.parent_run_id,
        )
        # PENDING first, so a crash right after this leaves a recoverable run.
        await self._run_store.save(run)

        token = CancellationToken()
        self._tokens[run.id] = token
        self._inboxes[run.id] = asyncio.Queue()
        if request.notes:
            self._inboxes[run.id].put_nowait(request.notes)  # drained before the first model call
        self._agent_names[run.id] = runtime.agent.name

        run.status = RunStatus.RUNNING
        await self._run_store.save(run)

        # A child run announces itself on the *parent's* run scope so the parent's stream
        # (and the TUI) learns a subagent exists. The child's own lifecycle events stream
        # on its own run id, tagged with parent_run_id.
        if request.parent_run_id is not None:
            await self._publish_subagent_started(
                child_run_id=run.id,
                agent_name=runtime.agent.name,
                parent_run_id=request.parent_run_id,
            )

        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._queues[run.id] = queue

        # The drive task waits for the pump to register with the bus before the runtime
        # publishes anything, so no event is ever lost to a late subscriber.
        registered = asyncio.Event()
        drive_task = asyncio.create_task(
            self._drive(run, request, token, queue, registered, runtime)
        )
        self._tasks[run.id] = drive_task
        # Mark the outcome as retrieved even if nobody awaits the task, so asyncio never
        # warns about an unretrieved exception; ``result()`` can still re-raise it.
        drive_task.add_done_callback(_consume_outcome)

        if self._event_bus is not None:
            pump_task = asyncio.create_task(self._pump(run.id, queue, registered))
            self._pumps[run.id] = pump_task

        return RunHandle(run=run, events=self._drain(queue), result=drive_task)

    async def _drive(
        self,
        run: Run,
        request: RunRequest,
        token: CancellationToken,
        queue: asyncio.Queue[AgentEvent | None],
        registered: asyncio.Event,
        runtime: AgentRuntime,
    ) -> AIMessage:
        """Run the runtime and transition the run to its terminal state."""
        try:
            if self._event_bus is not None:
                await registered.wait()
            context = RunContext(
                run_id=run.id,
                session_id=request.session_id,
                user_id=request.user_id,
                depth=request.depth,
                parent_run_id=request.parent_run_id,
                agent=runtime.agent,
                services=self,
            )
            with run_context(context):
                result = await runtime.run(
                    session_id=request.session_id,
                    user_id=request.user_id,
                    message=request.message,
                    run_id=run.id,
                    cancel=token,
                    counters=self._record_counters(run),
                    checkpoint_store=self._checkpoint_store,
                    parent_run_id=request.parent_run_id,
                    inbox=lambda: self._drain_inbox(run.id),
                )
            run.status = RunStatus.COMPLETED
            return result
        except asyncio.CancelledError:
            # Hard cancellation: the runtime publishes no terminal event, so end the
            # event pump explicitly (it closes the stream with a sentinel).
            run.status = RunStatus.CANCELLED
            self._stop_pump(run.id)
            raise
        except RunCancelled:
            # Cooperative cancellation: the runtime already published RUN_FAILED as its
            # terminal event; the pump ends on its own.
            run.status = RunStatus.CANCELLED
            raise
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = error_text(exc)
            raise
        finally:
            try:
                await self._run_store.save(run)
            finally:
                if request.parent_run_id is not None:
                    await self._publish_subagent_completed(run, request.parent_run_id)
                if self._event_bus is None:
                    # No pump to close it: end the event stream right here.
                    self._end_stream(run.id)
                self._finish(run.id)

    async def _pump(
        self,
        run_id: str,
        queue: asyncio.Queue[AgentEvent | None],
        registered: asyncio.Event,
    ) -> None:
        """Forward a run's bus events into ``queue`` in publish order.

        The pump is the queue's *single writer*: it forwards events until it sees a
        terminal event (the bus subscription ends on its own), then signals the stream
        end with ``None``. A hard-cancelled run never publishes a terminal event, so the
        drive task cancels the pump instead; the ``finally`` below still closes the
        stream, keeping ``events()`` from hanging.
        """
        assert self._event_bus is not None  # only created when a bus is wired
        stream = self._event_bus.subscribe(run_id)
        next_event = asyncio.ensure_future(stream.__anext__())
        try:
            # The subscription's queue registers with the bus on its first ``__anext__``
            # step. Give that step a tick, then let the drive task start publishing.
            await asyncio.sleep(0)
            registered.set()
            while True:
                try:
                    event = await next_event
                except StopAsyncIteration:
                    break
                # Live progress for the parent's status tool: the run store only learns
                # the final counts, so derive running counts from the event stream.
                progress = self._progress.setdefault(run_id, _Progress())
                if event.type is EventType.ASSISTANT_MESSAGE:
                    progress.steps += 1
                elif event.type is EventType.TOOL_COMPLETED:
                    progress.tools += 1
                progress.last_event = event.type.value
                await self._reflect_approval(event)
                queue.put_nowait(event)
                if event.type in _TERMINAL_EVENTS:
                    break
                next_event = asyncio.ensure_future(stream.__anext__())
        finally:
            if not next_event.done():
                next_event.cancel()
            # ``subscribe`` is typed as the async-iterator protocol, so close the
            # underlying generator defensively.
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
            queue.put_nowait(None)

    async def _reflect_approval(self, event: AgentEvent) -> None:
        """Mirror a pause for approval in the stored run status (and back to running)."""
        if event.type is EventType.APPROVAL_REQUIRED:
            status = RunStatus.WAITING_APPROVAL
        elif event.type is EventType.APPROVAL_RESOLVED:
            status = RunStatus.RUNNING
        else:
            return
        stored = await self._run_store.get(event.run_id)
        if stored is not None and stored.status not in _TERMINAL_RUN_STATUSES:
            stored.status = status
            await self._run_store.save(stored)

    async def _drain(self, queue: asyncio.Queue[AgentEvent | None]) -> AsyncIterator[AgentEvent]:
        """Yield a run's buffered events until the end-of-stream sentinel."""
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item

    async def cancel(self, run_id: str) -> None:
        """Cancel a run cooperatively, with ``task.cancel()`` as a backstop.

        Unknown or already-finished run ids are an idempotent no-op. Awaiting here
        guarantees the run's terminal state is persisted when ``cancel`` returns.
        """
        task = self._tasks.get(run_id)
        token = self._tokens.get(run_id)
        if task is None or token is None:
            return
        token.cancel()
        task.cancel()
        with contextlib.suppress(RunCancelled, asyncio.CancelledError, Exception):
            await task

        # A cancellation that lands before the drive task's first event-loop step never
        # runs its except/finally (the coroutine body is skipped entirely), so persist
        # the terminal state here whenever the run is still in flight.
        run = await self._run_store.get(run_id)
        if run is not None and run.status not in _TERMINAL_RUN_STATUSES:
            run.status = RunStatus.CANCELLED
            await self._run_store.save(run)
            if run.parent_run_id is not None:
                await self._publish_subagent_completed(run, run.parent_run_id)
            self._stop_pump(run_id)
            self._end_stream(run_id)
            self._finish(run_id)

    async def send_input(self, run_id: str, message: str) -> bool:
        """Inject ``message`` into a running run's inbox.

        Returns ``False`` for an unknown or already-finished run; the runtime drains the
        inbox at the top of each loop iteration (before the model call).
        """
        inbox = self._inboxes.get(run_id)
        if inbox is None:
            return False
        inbox.put_nowait(message)
        return True

    async def start_subagent(
        self,
        agent: Agent,
        *,
        parent_run_id: str,
        user_id: str,
        message: str,
        depth: int,
    ) -> SubagentRun:
        """Start a child run against the child agent's own runtime.

        The child gets its own session (so it never shares the parent's transcript or
        version), its own ``run_id``, and a ``parent_run_id`` back-link.
        """
        if self._agent_factory is None:
            raise RuntimeError("RunManager has no agent factory; cannot start a subagent")
        child_runtime = self._agent_factory.create(agent)
        request = RunRequest(
            session_id=f"{parent_run_id}::{agent.name}::{uuid4().hex}",
            user_id=user_id,
            message=message,
            parent_run_id=parent_run_id,
            depth=depth,
        )
        handle = await self._start(request, child_runtime)
        return SubagentRun(run_id=handle.run_id, result=handle.result())

    async def get_run_view(self, run_id: str) -> RunView | None:
        """A run's status, live step/tool counts, and last event (or ``None``)."""
        run = await self._run_store.get(run_id)
        if run is None:
            return None
        progress = self._progress.get(run_id)
        if run.status in _TERMINAL_RUN_STATUSES:
            steps, tools = run.step_count, run.tool_call_count
        else:
            steps = progress.steps if progress else 0
            tools = progress.tools if progress else 0
        return RunView(
            run_id=run.id,
            status=run.status.value,
            steps=steps,
            tool_calls=tools,
            last_event=progress.last_event if progress else None,
            error=run.error,
            parent_run_id=run.parent_run_id,
        )

    async def list_child_ids(self, parent_run_id: str) -> list[str]:
        """The ids of runs whose ``parent_run_id`` is ``parent_run_id``."""
        return [run.id for run in await self._run_store.list_runs(parent_run_id)]

    async def get_run(self, run_id: str) -> Run | None:
        """Fetch a run's persisted record (for the UI)."""
        return await self._run_store.get(run_id)

    async def list_children(self, parent_run_id: str) -> list[Run]:
        """Fetch the persisted records of a parent's child runs (for the UI)."""
        return await self._run_store.list_runs(parent_run_id)

    def _drain_inbox(self, run_id: str) -> list[str]:
        """Non-blocking read of a run's pending injected messages."""
        inbox = self._inboxes.get(run_id)
        if inbox is None:
            return []
        items: list[str] = []
        while not inbox.empty():
            items.append(inbox.get_nowait())
        return items

    async def _publish_subagent_started(
        self,
        *,
        child_run_id: str,
        agent_name: str,
        parent_run_id: str,
    ) -> None:
        if self._event_bus is None:
            return
        # This event lives on the parent's run scope, so it carries the parent's session
        # (not the child's) — every event's session_id matches its run_id.
        parent = await self._run_store.get(parent_run_id)
        event = AgentEvent.create(
            EventType.SUBAGENT_STARTED,
            run_id=parent_run_id,
            session_id=parent.session_id if parent is not None else parent_run_id,
            sequence=0,
            data={"child_run_id": child_run_id, "agent_name": agent_name},
        )
        with contextlib.suppress(Exception):
            await self._event_bus.publish(event)

    async def _publish_subagent_completed(self, run: Run, parent_run_id: str) -> None:
        if self._event_bus is None:
            return
        parent = await self._run_store.get(parent_run_id)
        event = AgentEvent.create(
            EventType.SUBAGENT_COMPLETED,
            run_id=parent_run_id,
            session_id=parent.session_id if parent is not None else run.session_id,
            sequence=0,
            data={
                "child_run_id": run.id,
                "agent_name": self._agent_names.get(run.id, run.id),
                "status": run.status.value,
                "steps": run.step_count,
                "tool_calls": run.tool_call_count,
            },
        )
        with contextlib.suppress(Exception):
            await self._event_bus.publish(event)

    @staticmethod
    def _record_counters(run: Run) -> Callable[[int, int], None]:
        """Capture a finished run's step/tool counts onto the ``Run`` record."""

        def record(steps: int, calls: int) -> None:
            run.step_count = steps
            run.tool_call_count = calls

        return record

    async def recover(self) -> int:
        """Mark every stale unfinished run FAILED (pending, running or awaiting approval).

        Called at startup after a crash: a run that never finished is not retried —
        it is recorded as failed so the run store reflects reality.
        """
        count = 0
        for run in await self._run_store.list_incomplete():
            run.status = RunStatus.FAILED
            run.error = "interrupted before startup recovery"
            await self._run_store.save(run)
            count += 1
        return count

    def _stop_pump(self, run_id: str) -> None:
        pump = self._pumps.get(run_id)
        if pump is not None:
            pump.cancel()

    def _end_stream(self, run_id: str) -> None:
        """Close a run's event stream so ``events()`` stops waiting.

        The sentinel is idempotent: ``_drain`` returns on the first ``None``, and a
        pump that is being cancelled delivers its own sentinel in its ``finally``.
        """
        queue = self._queues.pop(run_id, None)
        if queue is not None:
            queue.put_nowait(None)

    def _finish(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._tokens.pop(run_id, None)
        self._pumps.pop(run_id, None)
        self._queues.pop(run_id, None)
        self._inboxes.pop(run_id, None)
        self._progress.pop(run_id, None)
        self._agent_names.pop(run_id, None)


def _consume_outcome(task: asyncio.Task[AIMessage]) -> None:
    """Retrieve a finished task's exception so asyncio never warns about it.

    A later ``await task`` (via ``RunHandle.result``) still re-raises the outcome; this
    only marks it as retrieved for the ``Task was destroyed but it is pending!`` guard.
    """
    if not task.cancelled():
        with contextlib.suppress(asyncio.InvalidStateError):
            task.exception()
