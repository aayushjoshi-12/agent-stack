"""Human-in-the-loop approval for dangerous tool calls, independent of any gateway.

The runtime asks the :class:`ApprovalBroker` for a decision before it executes a gated tool
call and waits; a gateway shows the request and answers it through
``Application.decide_approval``. A decision is bound to the run, session and principal the
request was created for (a gateway cannot approve someone else's call) and can be given
exactly once, so an approval cannot be replayed against another run or a second time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from agent_stack.core.errors import ApprovalError

__all__ = ["ApprovalBroker", "ApprovalRequest", "ApprovalStatus", "ToolPolicy"]


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ToolPolicy:
    """Which tools need a human's approval. A plain name set: no command classification."""

    requires_approval: frozenset[str] = frozenset()

    def needs_approval(self, tool_name: str) -> bool:
        return tool_name in self.requires_approval


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
        """Block until the request is decided. A cancelled run marks it ``CANCELLED``."""
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
