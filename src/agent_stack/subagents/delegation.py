"""Delegation and parent-side control tools (Phase 20, ADR 005).

``delegate`` resolves a child :class:`~agent_stack.core.agent.Agent` from the *current*
run's agent ``subagents`` specs, starts a child run linked by ``parent_run_id``, awaits
it, and returns a compact summary. The three control tools let the parent observe and
steer a running child. All four read the ambient
:class:`~agent_stack.core.run_context.RunContext`; none imports the application or the
run manager. Tools report problems as short strings — they never raise.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool

from agent_stack.core.agent import Agent
from agent_stack.core.run import RunStatus
from agent_stack.core.run_context import RunView, SkillScope, current_run_context

__all__ = [
    "control_tools",
    "delegate",
    "inherit",
    "subagent_feed",
    "subagent_interrupt",
    "subagent_status",
]

#: A child paused for approval still occupies one of the parent's subagent slots.
_ACTIVE_CHILD_STATUSES = frozenset({RunStatus.RUNNING.value, RunStatus.WAITING_APPROVAL.value})
_TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)


def inherit(child: Agent, parent: Agent, scope: SkillScope) -> Agent:
    """``child`` cut down to what ``parent`` may do (P49): never a tool the parent lacks or its
    active skill blocks, and every call the parent would have to approve needs approval too."""
    names = {tool.name for tool in parent.tools}
    return replace(
        child,
        tools=tuple(t for t in child.tools if t.name in names and scope.allows(t.name)),
        tool_policy=parent.tool_policy.union(child.tool_policy),
    )


def _content_text(message: AIMessage) -> str:
    content = message.content
    return content if isinstance(content, str) else str(content)


def _summary(agent_name: str, run_id: str, view: RunView | None, text: str) -> str:
    status = view.status if view is not None else RunStatus.FAILED.value
    steps = view.steps if view is not None else 0
    tools = view.tool_calls if view is not None else 0
    headline = (
        f"Subagent {agent_name!r} (run {run_id}) {status}: {steps} steps, {tools} tool calls."
    )
    body = text.strip() or "(no output)"
    return f"{headline}\n{body}"


@tool
async def delegate(agent_name: str, task: str) -> str:
    """Delegate a self-contained task to a named subagent and get back a short summary.

    The subagent runs as its own run linked to the current one; its events stream to the
    UI tagged with this run's id. Use it to hand off read-only exploration or a
    self-contained job instead of doing all the work inline. Only the summary returns —
    never the subagent's full transcript.
    """
    ctx = current_run_context()
    if ctx is None:
        return "Error: delegate must be called from inside a run."

    limits = ctx.agent.limits
    if ctx.depth + 1 > limits.max_subagent_depth:
        return (
            f"Error: subagent depth limit reached (max {limits.max_subagent_depth}); "
            "cannot delegate further."
        )

    spec = next((spec for spec in ctx.agent.subagents if spec.name == agent_name), None)
    if spec is None:
        available = ", ".join(spec.name for spec in ctx.agent.subagents) or "(none)"
        return f"Error: unknown subagent {agent_name!r}. Available: {available}."

    running = 0
    for child_id in await ctx.services.list_child_ids(ctx.run_id):
        view = await ctx.services.get_run_view(child_id)
        if view is not None and view.status in _ACTIVE_CHILD_STATUSES:
            running += 1
    if running >= limits.max_subagents:
        return (
            f"Error: subagent limit reached ({limits.max_subagents} running); "
            "wait for one to finish."
        )

    try:
        child = await ctx.services.start_subagent(
            inherit(spec.agent, ctx.agent, ctx.skill_scope),
            parent_run_id=ctx.run_id,
            user_id=ctx.user_id,
            message=task,
            depth=ctx.depth + 1,
        )
    except Exception as exc:
        return f"Error: could not start subagent {agent_name!r}: {exc}"

    text = ""
    try:
        text = _content_text(await child.result)
    except asyncio.CancelledError:
        # A child interrupt surfaces here as CancelledError on the child's result — report
        # it, don't fail the parent. But if *this* run is the one being cancelled, the
        # error is ours to propagate (never swallow your own cancellation).
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        text = ""
    except Exception as exc:
        text = f"({exc})"

    view = await ctx.services.get_run_view(child.run_id)
    return _summary(agent_name, child.run_id, view, text)


# A subagent run is bounded by its own limits and the parent's run deadline, not the
# per-call tool timeout, which would cut a long but healthy subagent off mid-run.
delegate.metadata = {"timeout_seconds": None}


@tool
async def subagent_status(run_id: str) -> str:
    """Report a subagent run's status, step/tool counts, and most recent activity."""
    ctx = current_run_context()
    if ctx is None:
        return "Error: no active run."
    view = await ctx.services.get_run_view(run_id)
    if view is None:
        return f"Error: unknown run {run_id!r}."
    if view.parent_run_id != ctx.run_id:
        return f"Error: run {run_id!r} is not a subagent of this run."
    parts = [
        f"run {run_id}: {view.status}",
        f"steps={view.steps}",
        f"tool_calls={view.tool_calls}",
    ]
    if view.last_event:
        parts.append(f"last={view.last_event}")
    if view.error:
        parts.append(f"error={view.error}")
    return " ".join(parts)


@tool
async def subagent_interrupt(run_id: str) -> str:
    """Cancel a running subagent; the current run then sees ``subagent.completed``."""
    ctx = current_run_context()
    if ctx is None:
        return "Error: no active run."
    view = await ctx.services.get_run_view(run_id)
    if view is None:
        return f"Error: unknown run {run_id!r}."
    if view.parent_run_id != ctx.run_id:
        return f"Error: run {run_id!r} is not a subagent of this run."
    if view.status in _TERMINAL_STATUSES:
        return f"Subagent run {run_id} already {view.status}."
    await ctx.services.cancel(run_id)
    return f"Interrupted subagent run {run_id}."


@tool
async def subagent_feed(run_id: str, message: str) -> str:
    """Send a message into a running subagent's context (cooperative injection)."""
    ctx = current_run_context()
    if ctx is None:
        return "Error: no active run."
    view = await ctx.services.get_run_view(run_id)
    if view is None:
        return f"Error: unknown run {run_id!r}."
    if view.parent_run_id != ctx.run_id:
        return f"Error: run {run_id!r} is not a subagent of this run."
    if not await ctx.services.send_input(run_id, message):
        return f"Error: subagent run {run_id} is not accepting input."
    return f"Sent input to subagent run {run_id}."


def control_tools() -> tuple[BaseTool, ...]:
    """The delegation tool plus the parent-side control tools, ready to bind."""
    return (delegate, subagent_status, subagent_interrupt, subagent_feed)
