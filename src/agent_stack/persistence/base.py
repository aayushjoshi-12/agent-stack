"""Store protocols, re-exported for convenient import.

Thin on purpose: the canonical definitions live next to their domain (``core``) or their
implementation (``persistence.conversation``); this module only gathers the infrastructure
boundaries so callers can ``from agent_stack.persistence.base import SessionStore``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_stack.core.run import Run
from agent_stack.core.session import Session, SessionStore
from agent_stack.persistence.conversation import ConversationStore
from agent_stack.persistence.serializers import MessageRecord

__all__ = [
    "ConversationStore",
    "MessageRecord",
    "RunStore",
    "Session",
    "SessionStore",
]


@runtime_checkable
class RunStore(Protocol):
    """Infrastructure boundary for run lifecycle records."""

    async def save(self, run: Run) -> None: ...

    async def get(self, run_id: str) -> Run | None: ...

    async def list_incomplete(self) -> list[Run]:
        """Return runs still pending or running (startup recovery dropdown)."""
        ...
