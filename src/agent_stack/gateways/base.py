"""The gateway contract: one inbound shape, one outbound protocol.

Every gateway converts its native protocol into a :class:`MessageEnvelope`,
drives the :class:`~agent_stack.app.application.Application` with it, and renders
the :class:`AgentEvent` stream that comes back. This module is the only place
that contract is spelled out; concrete gateways (CLI, TUI, Telegram, Web) live in
their own submodules.

The identity seam — ``gateway identity → principal → session`` — is provided by
the application's resolvers (:class:`IdentityResolver` /
:class:`SessionResolver`); a gateway uses them to build an envelope whose
``principal`` and ``session`` are already resolved.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agent_stack.core.events import AgentEvent

if TYPE_CHECKING:
    from agent_stack.app.application import Application

__all__ = ["Gateway", "HistoryItem", "MessageEnvelope", "SessionSummary"]


@dataclass(frozen=True)
class MessageEnvelope:
    """One inbound message, already resolved to the application boundary.

    ``principal`` is the resolved owner id, ``session`` the session to run in
    (the runtime creates the session lazily on first use), and ``text`` the
    message. ``metadata`` carries opaque gateway extras (idempotency keys, native
    message ids) that no consumer needs yet.
    """

    principal: str
    session: str
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionSummary:
    """One conversation in a picker: its id and a human title (first message)."""

    id: str
    title: str


@dataclass(frozen=True)
class HistoryItem:
    """One row of a stored conversation, in a gateway-neutral shape.

    ``kind`` is ``user``, ``assistant`` or ``tool``; ``text`` is the message text or the
    tool name, and ``args`` carries a tool call's arguments.
    """

    kind: str
    text: str
    args: Mapping[str, Any] | None = None


@runtime_checkable
class Gateway(Protocol):
    """A gateway drives the Application and consumes AgentEvents.

    Implementations convert their native protocol into MessageEnvelopes and
    render the returned event stream. A gateway never touches tools, models, or
    stores directly — it imports the Application and this contract only.
    """

    async def handle_message(
        self,
        application: Application,
        envelope: MessageEnvelope,
    ) -> AsyncIterator[AgentEvent]:
        """Dispatch one message through the Application and stream its events."""
        ...
