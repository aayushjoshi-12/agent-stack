from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from langchain_core.messages import BaseMessage


@dataclass
class Session:
    """Durable conversation identity and metadata.

    A ``Session`` owns the conversation *identity*, not the execution state. It is a
    persistent object, so it is mutable; callers persist changes via ``SessionStore.save``.
    """

    id: str
    owner_id: str
    agent_name: str
    title: str | None = None
    status: str = "active"
    version: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionStore(Protocol):
    """Infrastructure boundary for session metadata and conversation messages."""

    async def get(self, session_id: str) -> Session | None: ...

    async def save(self, session: Session) -> None: ...

    async def get_messages(self, session_id: str) -> list[BaseMessage]: ...

    async def append_messages(self, session_id: str, messages: Sequence[BaseMessage]) -> None: ...

    async def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """Volatile ``SessionStore`` used by tests and the first composition root.

    Reads return copies so callers can never mutate the store's internal state in place.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._messages: dict[str, list[BaseMessage]] = {}

    async def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return replace(session, metadata=dict(session.metadata))

    async def save(self, session: Session) -> None:
        self._sessions[session.id] = replace(session, metadata=dict(session.metadata))

    async def get_messages(self, session_id: str) -> list[BaseMessage]:
        return list(self._messages.get(session_id, []))

    async def append_messages(self, session_id: str, messages: Sequence[BaseMessage]) -> None:
        self._messages.setdefault(session_id, []).extend(messages)

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._messages.pop(session_id, None)
