"""Human-in-the-loop approval for dangerous tool calls, independent of any gateway.

The runtime asks the :class:`ApprovalBroker` for a decision before it executes a gated tool
call and waits; a gateway shows the request and answers it through
``Application.decide_approval``. A decision is bound to the run, session and principal the
request was created for (a gateway cannot approve someone else's call) and can be given
exactly once, so an approval cannot be replayed against another run or a second time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from agent_stack.core.errors import ApprovalError

__all__ = [
    "APPROVAL_MODES",
    "Advice",
    "ApprovalAdvisor",
    "ApprovalBroker",
    "ApprovalMode",
    "ApprovalRequest",
    "ApprovalStatus",
    "ToolPolicy",
    "Verdict",
]

APPROVAL_MODES = ("ask", "auto")


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ToolPolicy:
    """Which tool calls need approval: named tools, plus calls a rule flags by their arguments.

    ``dynamic`` sees the tool name and its arguments and answers True for a call that must be
    approved even though the tool is not gated by name (a read of a file outside the workspace).
    """

    requires_approval: frozenset[str] = frozenset()
    dynamic: Callable[[str, Mapping[str, Any]], bool] | None = None

    def needs_approval(self, tool_name: str, args: Mapping[str, Any] | None = None) -> bool:
        if tool_name in self.requires_approval:
            return True
        return self.dynamic is not None and args is not None and self.dynamic(tool_name, args)

    def union(self, other: ToolPolicy) -> ToolPolicy:
        """A call needs approval if either policy says so (a subagent under its parent, P49)."""
        dynamic = [d for d in (self.dynamic, other.dynamic) if d is not None]
        return ToolPolicy(
            self.requires_approval | other.requires_approval,
            dynamic=(lambda tool, args: any(d(tool, args) for d in dynamic)) if dynamic else None,
        )


class Verdict(StrEnum):
    """What an advisor says about a gated call: run it, refuse it, or leave it to the human."""

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class Advice:
    verdict: Verdict
    reason: str = ""


class ApprovalAdvisor(Protocol):
    """Decides gated calls on the user's behalf (auto mode). It never has the last word on
    ``ASK``: that goes to the human exactly as without an advisor."""

    async def advise(
        self, tool: str, args: Mapping[str, Any], *, request: str | None
    ) -> Advice: ...


class ApprovalMode:
    """Who answers a gated call first: ``ask`` is always the human, ``auto`` the advisor.

    A tiny shared switch, so a gateway can flip it while a conversation is running.
    """

    def __init__(self, mode: str = "ask") -> None:
        self._mode = "ask"
        self.set(mode)

    @property
    def value(self) -> str:
        return self._mode

    def set(self, mode: str) -> None:
        if mode not in APPROVAL_MODES:
            raise ApprovalError(f"approval mode must be one of {', '.join(APPROVAL_MODES)}")
        self._mode = mode


@dataclass
class ApprovalRequest:
    """One pending decision about one tool call, bound to its run, session and principal."""

    id: str
    run_id: str
    session_id: str
    principal: str
    tool: str
    tool_call_id: str
    args: Mapping[str, Any] = field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING


class ApprovalBroker:
    """Creates requests, hands out decisions, and wakes the run that is waiting."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._waiters: dict[str, asyncio.Future[ApprovalStatus]] = {}

    def create(
        self,
        *,
        run_id: str,
        session_id: str,
        principal: str,
        tool: str,
        tool_call_id: str,
        args: Mapping[str, Any],
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            id=uuid4().hex,
            run_id=run_id,
            session_id=session_id,
            principal=principal,
            tool=tool,
            tool_call_id=tool_call_id,
            args=dict(args),
        )
        self._requests[request.id] = request
        self._waiters[request.id] = asyncio.get_running_loop().create_future()
        return request

    async def wait(self, approval_id: str) -> ApprovalStatus:
        """Block until the request is decided. A cancelled run marks it ``CANCELLED``.

        Either way the request is then forgotten (the run has its answer), so the broker holds
        only undecided ones; deciding a forgotten request is refused as unknown.
        """
        waiter = self._waiters[approval_id]
        try:
            return await waiter
        except asyncio.CancelledError:
            request = self._requests[approval_id]
            if request.status is ApprovalStatus.PENDING:
                request.status = ApprovalStatus.CANCELLED
            raise
        finally:
            self._waiters.pop(approval_id, None)
            self._requests.pop(approval_id, None)

    def decide(
        self,
        approval_id: str,
        *,
        run_id: str,
        session_id: str,
        principal: str,
        approve: bool,
    ) -> ApprovalRequest:
        """Record a decision after checking it belongs to this run, session and principal."""
        request = self._requests.get(approval_id)
        if request is None:
            raise ApprovalError(f"unknown approval {approval_id}")
        if (request.run_id, request.session_id, request.principal) != (
            run_id,
            session_id,
            principal,
        ):
            raise ApprovalError("approval does not belong to this run, session and principal")
        if request.status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"approval already {request.status.value}")
        request.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        waiter = self._waiters.get(approval_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(request.status)
        return request

    def pending(self, run_id: str) -> list[ApprovalRequest]:
        return [
            r
            for r in self._requests.values()
            if r.run_id == run_id and r.status is ApprovalStatus.PENDING
        ]
