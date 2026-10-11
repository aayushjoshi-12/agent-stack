"""Run lifecycle: ``Run`` / ``RunStatus`` plus the run manager and handle.

A :class:`Run` owns execution *lifecycle* only — never conversation content. The
:class:`RunManager` turns a :class:`RunRequest` into a started run, exposes a live
:class:`RunHandle` (events + result), and can cancel or recover runs. The manager is the
only place that drives the runtime with a ``run_id`` and a cancellation token.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import uuid4

from langchain_core.messages import AIMessage

from agent_stack.core.cancellation import CancellationToken
from agent_stack.core.errors import RunCancelled
from agent_stack.core.event_bus import EventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.runtime import AgentRuntime

if TYPE_CHECKING:
    from agent_stack.persistence.base import RunStore

__all__ = ["Run", "RunHandle", "RunManager", "RunRequest", "RunStatus"]

_TERMINAL_EVENTS = frozenset({EventType.RUN_COMPLETED, EventType.RUN_FAILED})


class RunStatus(StrEnum):
    """Lifecycle states of a single agent run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})


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
    ) -> None:
        self._runtime = runtime
        self._run_store = run_store
        self._event_bus = event_bus

        self._tasks: dict[str, asyncio.Task[AIMessage]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._queues: dict[str, asyncio.Queue[AgentEvent | None]] = {}

    async def start(self, request: RunRequest) -> RunHandle:
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

        run.status = RunStatus.RUNNING
        await self._run_store.save(run)

        queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self._queues[run.id] = queue

        # The drive task waits for the pump to register with the bus before the runtime
        # publishes anything, so no event is ever lost to a late subscriber.
        registered = asyncio.Event()
        drive_task = asyncio.create_task(self._drive(run, request, token, queue, registered))
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
    ) -> AIMessage:
        """Run the runtime and transition the run to its terminal state."""
        try:
            if self._event_bus is not None:
                await registered.wait()
            result = await self._runtime.run(
                session_id=request.session_id,
                user_id=request.user_id,
                message=request.message,
                run_id=run.id,
                cancel=token,
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
            run.error = repr(exc)
            raise
        finally:
            try:
                await self._run_store.save(run)
            finally:
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
            self._stop_pump(run_id)
            self._end_stream(run_id)
            self._finish(run_id)

    async def recover(self) -> int:
        """Mark every stale PENDING/RUNNING run FAILED; return how many were marked.

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


def _consume_outcome(task: asyncio.Task[AIMessage]) -> None:
    """Retrieve a finished task's exception so asyncio never warns about it.

    A later ``await task`` (via ``RunHandle.result``) still re-raises the outcome; this
    only marks it as retrieved for the ``Task was destroyed but it is pending!`` guard.
    """
    if not task.cancelled():
        with contextlib.suppress(asyncio.InvalidStateError):
            task.exception()