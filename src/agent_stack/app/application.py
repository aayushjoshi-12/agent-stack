"""The application service layer: the stable boundary every gateway talks to.

A gateway drives a full run with :class:`Application` alone — ``send_message`` →
:class:`~agent_stack.core.run.RunHandle` (events + result), ``cancel_run``,
``get_session``, ``subscribe`` — and never imports ``core`` directly. The
identity seam (:class:`IdentityResolver`, :class:`SessionResolver`) maps a
gateway's native identity to a principal and that principal to its default
session; gateways use it to build :class:`~agent_stack.gateways.base.MessageEnvelope`\\ s.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent
from agent_stack.core.approval import ApprovalBroker
from agent_stack.core.errors import SessionVersionConflictError
from agent_stack.core.event_bus import EventBus
from agent_stack.core.events import AgentEvent
from agent_stack.core.factory import AgentFactory
from agent_stack.core.run import Run, RunHandle, RunManager, RunRequest
from agent_stack.core.session import Session, SessionStore
from agent_stack.gateways.base import HistoryItem, MessageEnvelope, SessionSummary
from agent_stack.observability.metrics import Metrics


@dataclass
class Application:
    """The stable boundary gateways talk to.

    It exposes the wired object graph; gateways never import ``core`` directly.
    """

    config: AppConfig
    agent: Agent
    factory: AgentFactory
    session_store: SessionStore
    run_manager: RunManager
    event_bus: EventBus
    approvals: ApprovalBroker
    metrics: Metrics

    async def send_message(
        self,
        envelope: MessageEnvelope,
        *,
        parent_run_id: str | None = None,
    ) -> RunHandle:
        """Start a run for one inbound message and hand back its handle.

        Session creation stays lazy: the runtime creates the session on first
        use. ``parent_run_id`` links subagent runs to their parent (Phase 20).
        """
        request = RunRequest(
            session_id=envelope.session,
            user_id=envelope.principal,
            message=envelope.text,
            parent_run_id=parent_run_id,
        )
        return await self.run_manager.start(request)

    async def cancel_run(self, run_id: str) -> None:
        """Cancel a running run; unknown or finished runs are an idempotent no-op."""
        await self.run_manager.cancel(run_id)

    async def send_input(self, run_id: str, message: str) -> bool:
        """Inject a message into a running run (P20); ``False`` for unknown/finished."""
        return await self.run_manager.send_input(run_id, message)

    async def get_run(self, run_id: str) -> Run | None:
        """Fetch a run's persisted record, or ``None``."""
        return await self.run_manager.get_run(run_id)

    async def child_runs(self, parent_run_id: str) -> list[Run]:
        """A parent run's child runs (subagent listing for the UI)."""
        return await self.run_manager.list_children(parent_run_id)

    async def get_session(self, session_id: str) -> Session | None:
        """Return a session's metadata, or ``None`` when it does not exist."""
        return await self.session_store.get(session_id)

    def decide_approval(
        self,
        approval_id: str,
        *,
        run_id: str,
        session_id: str,
        principal: str,
        approve: bool,
    ) -> None:
        """Answer a pending ``approval.required`` request.

        Raises :class:`~agent_stack.core.errors.ApprovalError` if the id is unknown, was
        issued for a different run, session or principal, or was already decided.
        """
        self.approvals.decide(
            approval_id,
            run_id=run_id,
            session_id=session_id,
            principal=principal,
            approve=approve,
        )

    async def new_session(self, principal: str) -> str:
        """Create an empty session for ``principal`` and return its id."""
        session = Session(
            id=f"{principal}-{uuid4().hex[:8]}",
            owner_id=principal,
            agent_name=self.agent.name,
        )
        await self.session_store.save(session)
        return session.id

    async def list_sessions(self, principal: str, limit: int = 30) -> list[SessionSummary]:
        """The principal's sessions, most recently active first, for a picker."""
        sessions = await self.session_store.list_for_owner(principal, limit)
        return [SessionSummary(id=s.id, title=s.title or "Untitled") for s in sessions]

    async def delete_session(self, session_id: str) -> None:
        """Permanently delete a session and its stored conversation (idempotent)."""
        await self.session_store.delete(session_id)

    async def set_session_title(self, session_id: str, title: str) -> None:
        """Name a session once (the first message); an existing title is kept."""
        session = await self.session_store.get(session_id)
        if session is None or session.title:
            return
        session.title = title
        # A run may bump the version first; the title is cosmetic, so a conflict is skipped.
        with contextlib.suppress(SessionVersionConflictError):
            await self.session_store.save(session, expected_version=session.version)

    async def session_history(self, session_id: str) -> list[HistoryItem]:
        """A stored conversation as display rows: user text, assistant text, tool calls."""
        messages = await self.session_store.get_messages(session_id)
        results = {
            m.tool_call_id: _text_of(m) for m in messages if isinstance(m, ToolMessage)
        }
        return [item for message in messages for item in _history_items(message, results)]

    def subscribe(self, run_id: str) -> AsyncIterator[AgentEvent]:
        """A live event stream for a run (single-consumption, no replay)."""
        return self.event_bus.subscribe(run_id)


class IdentityResolver:
    """Map a gateway's external identity to a stable principal id.

    Owner-based only: ``gateway:external_user_id`` is the principal. No
    authorization depth — an approval is checked against its own run, session and principal
    (``ApprovalBroker.decide``), not against a session owner table.
    """

    def resolve(self, gateway: str, external_user_id: str) -> str:
        return f"{gateway}:{external_user_id}"


class SessionResolver:
    """Map a principal to its default session, creating it on first use.

    Owner-based: one default session per principal. Sessions are keyed by the
    principal id, so the mapping is deterministic and restart-safe.
    """

    def __init__(self, session_store: SessionStore, agent_name: str) -> None:
        self._session_store = session_store
        self._agent_name = agent_name

    async def resolve(self, principal_id: str) -> str:
        """Return the principal's session id; create the default session if absent."""
        session = await self._session_store.get(principal_id)
        if session is None:
            session = Session(
                id=principal_id,
                owner_id=principal_id,
                agent_name=self._agent_name,
            )
            await self._session_store.save(session)
        return session.id


def _text_of(message: BaseMessage) -> str:
    """A message's text, whether ``content`` is a string or a list of content blocks."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = [
        block if isinstance(block, str) else str(block.get("text", ""))
        for block in content
        if isinstance(block, (str, dict))
    ]
    return "".join(parts)


def _history_items(message: BaseMessage, results: dict[str, str] | None = None) -> list[HistoryItem]:
    """Map one stored message to display rows; a tool row carries its call's result."""
    results = results or {}
    if isinstance(message, HumanMessage):
        text = _text_of(message)
        return [HistoryItem("user", text)] if text.strip() else []
    if isinstance(message, AIMessage):
        items: list[HistoryItem] = []
        text = _text_of(message)
        if text.strip():
            items.append(HistoryItem("assistant", text))
        items.extend(
            HistoryItem(
                "tool",
                str(call["name"]),
                dict(call.get("args") or {}),
                results.get(str(call.get("id"))),
            )
            for call in message.tool_calls
        )
        return items
    return []
